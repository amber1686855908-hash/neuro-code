from __future__ import annotations

import asyncio
import multiprocessing
import os
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

from neuro_code.application.ports.model import ModelProvider
from neuro_code.application.ports.task_dag import TaskDagError
from neuro_code.application.ports.task_dag_recovery import TaskDagRecoveryClaimError
from neuro_code.application.sessions.binding import ConversationBinding, ConversationRunner
from neuro_code.application.workflows.task_dag import (
    CreateTaskDagRequest,
    RunTaskDagRequest,
    RunTaskDagStepRequest,
    RunTaskDagWaveRequest,
    TaskDagApplicationService,
    TaskDagWritableService,
)
from neuro_code.application.workflows.writable_subagent import WritableSubagentExecutionIdentity
from neuro_code.domain.checkpoints import CheckpointId
from neuro_code.domain.session_tasks import SessionTask, SessionTaskKind, SessionTaskStatus
from neuro_code.domain.task_dag import (
    MAX_TASK_DAG_ERROR_BYTES,
    MAX_TASK_DAG_NODE_DEPENDENCIES,
    MAX_TASK_DAG_NODES,
    MAX_TASK_DAG_PARALLELISM,
    MAX_TASK_DAG_PROMPT_BYTES,
    MAX_TASK_DAG_RESPONSE_PREVIEW_BYTES,
    TaskDag,
    TaskDagNode,
    TaskDagNodeKind,
    TaskDagNodeState,
    TaskDagState,
)
from neuro_code.domain.task_dag_recovery import TaskDagRecoveryClaim
from neuro_code.domain.task_dag_result_relay import TaskDagDependencyResultRelay
from neuro_code.domain.worktree import WorktreeId, WorktreeRepositoryIdentity
from neuro_code.domain.writable_subagent import (
    WritableSubagentWorkspaceLease,
    WritableSubagentWorkspaceState,
)
from neuro_code.infrastructure.persistence.sqlite_session import SCHEMA_VERSION, SqliteSessionStore
from neuro_code.shared.errors import ConfigurationError


def _now() -> datetime:
    return datetime.now(UTC)


def _node(node_id: str, ordinal: int, dependencies: tuple[str, ...] = ()) -> TaskDagNode:
    return TaskDagNode(
        node_id=node_id,
        ordinal=ordinal,
        prompt=f"prompt-{node_id}",
        dependencies=dependencies,
    )


def _binding(session_id: str) -> ConversationBinding:
    class Runner:
        @property
        def session_id(self) -> str:
            return session_id

    return ConversationBinding(
        cast(ConversationRunner, Runner()),
        cast(ModelProvider, object()),
    )


class _FakeLeaseStore:
    def __init__(self) -> None:
        self.by_parent_task: dict[str, object] = {}

    async def get_writable_subagent_lease_for_parent_task(
        self,
        parent_session_id: str,
        parent_task_id: str,
    ) -> object | None:
        del parent_session_id
        return self.by_parent_task.get(parent_task_id)


class _FakeRelayStore:
    async def get_parent_context_relay_for_lease(self, lease_id: str) -> object:
        return SimpleNamespace(relay_id=f"relay-for-{lease_id}")


class _FakeDependencyRelayStore:
    def __init__(self) -> None:
        self.by_id: dict[str, TaskDagDependencyResultRelay] = {}
        self.by_target: dict[tuple[str, str, int], TaskDagDependencyResultRelay] = {}

    async def initialize(self) -> None:
        return None

    async def insert_task_dag_dependency_relay(
        self,
        relay: TaskDagDependencyResultRelay,
    ) -> TaskDagDependencyResultRelay:
        existing = self.by_id.get(relay.relay_id) or self.by_target.get(
            (relay.dag_id, relay.target_node_id, relay.target_node_generation)
        )
        if existing is not None:
            return existing
        self.by_id[relay.relay_id] = relay
        self.by_target[(relay.dag_id, relay.target_node_id, relay.target_node_generation)] = relay
        return relay

    async def get_task_dag_dependency_relay(
        self,
        relay_id: str,
    ) -> TaskDagDependencyResultRelay | None:
        return self.by_id.get(relay_id)

    async def get_task_dag_dependency_relay_for_target(
        self,
        dag_id: str,
        target_node_id: str,
        target_node_generation: int,
    ) -> TaskDagDependencyResultRelay | None:
        return self.by_target.get((dag_id, target_node_id, target_node_generation))


class _IntermittentClaimStore:
    def __init__(self, delegate: SqliteSessionStore, error: TaskDagError) -> None:
        self._delegate = delegate
        self._error = error
        self._raised = False

    async def claim_task_dag_node(self, *args, **kwargs):
        if not self._raised:
            self._raised = True
            raise self._error
        return await self._delegate.claim_task_dag_node(*args, **kwargs)

    def __getattr__(self, name: str) -> object:
        return getattr(self._delegate, name)


class _FakeWritableService:
    def __init__(
        self,
        parent_session_id: str,
        outcomes: dict[str, str | BaseException] | None = None,
        *,
        block: bool = False,
    ) -> None:
        self._parent_session_id = parent_session_id
        self.outcomes = outcomes or {}
        self.block = block
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls: list[tuple[str, str, str]] = []
        self.execution_identities: list[WritableSubagentExecutionIdentity] = []
        self.active = 0
        self.max_active = 0

    @property
    def parent_session_id(self) -> str:
        return self._parent_session_id

    async def initialize(self) -> None:
        return None

    async def reconcile_writable_subagent_workspaces(self) -> tuple[object, ...]:
        return ()

    async def run_subagent_with_execution_identity(
        self,
        request,
        *,
        execution_identity,
        sink=None,
    ) -> object:
        del sink
        self.execution_identities.append(execution_identity)
        self.calls.append(
            (request.prompt, execution_identity.node_id, execution_identity.parent_task_id)
        )
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.started.set()
        try:
            if self.block:
                await self.release.wait()
            await asyncio.sleep(0)
            outcome = self.outcomes.get(request.prompt, request.prompt)
            if isinstance(outcome, BaseException):
                raise outcome
            return SimpleNamespace(
                status=SessionTaskStatus.COMPLETED,
                response=outcome,
            )
        finally:
            self.active -= 1


class _ParallelRunState:
    def __init__(self, blocked_nodes: set[str] = ()) -> None:
        self.blocked_nodes = set(blocked_nodes)
        self.started_nodes: list[str] = []
        self.completed_nodes: list[str] = []
        self.invocation_count: dict[str, int] = {}
        self.dependency_relays: dict[str, TaskDagDependencyResultRelay] = {}
        self.active = 0
        self.max_active = 0
        self.started_event = asyncio.Event()
        self.release = asyncio.Event()
        self._lock = asyncio.Lock()

    async def mark_started(self, node_id: str) -> None:
        async with self._lock:
            self.started_nodes.append(node_id)
            self.invocation_count[node_id] = self.invocation_count.get(node_id, 0) + 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            if self.blocked_nodes.issubset(self.started_nodes):
                self.started_event.set()

    async def mark_completed(self, node_id: str) -> None:
        async with self._lock:
            self.completed_nodes.append(node_id)
            self.active -= 1


class _ParallelWritableService:
    def __init__(
        self,
        parent_session_id: str,
        state: _ParallelRunState,
        evidence_factory,
        failures: set[str] = (),
    ) -> None:
        self._parent_session_id = parent_session_id
        self._state = state
        self._evidence_factory = evidence_factory
        self._failures = set(failures)
        self.service_id = object()

    @property
    def parent_session_id(self) -> str:
        return self._parent_session_id

    async def initialize(self) -> None:
        return None

    async def reconcile_writable_subagent_workspaces(self) -> tuple[object, ...]:
        return ()

    async def run_subagent_with_execution_identity(
        self,
        request,
        *,
        execution_identity,
        sink=None,
    ) -> object:
        del sink
        node_id = execution_identity.node_id
        await self._state.mark_started(node_id)
        self._evidence_factory(execution_identity.parent_task_id, node_id)
        if request.dependency_result_relay is not None:
            self._state.dependency_relays[node_id] = request.dependency_result_relay
        if node_id in self._state.blocked_nodes:
            await self._state.release.wait()
        await asyncio.sleep(0)
        await self._state.mark_completed(node_id)
        if node_id in self._failures:
            return SimpleNamespace(
                status=SessionTaskStatus.FAILED,
                response=f"failed-{node_id}",
            )
        return SimpleNamespace(
            status=SessionTaskStatus.COMPLETED,
            response=f"completed-{request.prompt}",
        )


class _ParallelWritableFactory:
    def __init__(
        self,
        parent_session_id: str,
        state: _ParallelRunState,
        evidence_factory,
        failures: set[str] = (),
    ) -> None:
        self.parent_session_id = parent_session_id
        self.state = state
        self.evidence_factory = evidence_factory
        self.failures = set(failures)
        self.services: list[_ParallelWritableService] = []

    def create(self) -> _ParallelWritableService:
        service = _ParallelWritableService(
            self.parent_session_id,
            self.state,
            self.evidence_factory,
            self.failures,
        )
        self.services.append(service)
        return service


def _spawn_parallel_claim_worker(
    database_path: str, dag_id: str, node_id: str, barrier, queue
) -> None:
    async def claim() -> str:
        store = SqliteSessionStore(Path(database_path))
        await store.initialize()
        snapshot = await store.get_task_dag(dag_id)
        if snapshot is None:
            return "missing"
        proposed = replace(
            snapshot.node(node_id),
            state=TaskDagNodeState.RUNNING,
            generation=snapshot.node(node_id).generation + 1,
            parent_task_id=f"spawn-worker-{node_id}",
        )
        barrier.wait()
        try:
            await store.claim_task_dag_node(
                dag_id,
                proposed,
                expected_generation=snapshot.node(node_id).generation,
                expected_state=TaskDagNodeState.READY,
                updated_at=_now(),
            )
        except TaskDagError as error:
            return f"error:{error.kind}"
        return "claimed"

    queue.put(asyncio.run(claim()))


class TaskDagDomainTests(unittest.TestCase):
    def test_recovery_claim_validates_identity_and_versions(self) -> None:
        digest = "a" * 64
        claim = TaskDagRecoveryClaim.create(
            parent_session_id="parent",
            dag_id="dag",
            dag_definition_fingerprint=digest,
            node_id="node",
            node_generation=1,
            node_definition_fingerprint=digest,
            parent_task_id="task",
            dependency_relay_id="relay",
            dependency_relay_source_fingerprint=digest,
            dependency_relay_content_fingerprint=digest,
            dependency_relay_integrity_fingerprint=digest,
            owner_pid=123,
            owner_token="owner",
            created_at=_now(),
        )
        self.assertTrue(
            claim.same_execution(
                claim.with_owner(
                    owner_pid=456,
                    owner_token="new-owner",
                    version=1,
                    updated_at=claim.created_at + timedelta(seconds=1),
                )
            )
        )
        invalid_claims = (
            ({"claim_id": ""}, "safe identifier"),
            ({"dag_definition_fingerprint": "bad"}, "SHA-256"),
            ({"node_generation": True}, "node generation must be an integer"),
            ({"node_generation": -1}, "node generation must be non-negative"),
            ({"owner_pid": True}, "owner PID must be an integer"),
            ({"owner_pid": 0}, "owner PID must be positive"),
            ({"version": True}, "claim version must be an integer"),
            ({"version": -1}, "claim version must be non-negative"),
            ({"created_at": cast(datetime, object())}, "creation time must be timezone-aware"),
            ({"updated_at": cast(datetime, object())}, "update time must be timezone-aware"),
            (
                {"updated_at": claim.created_at - timedelta(seconds=1)},
                "must not precede creation",
            ),
        )
        for changes, message in invalid_claims:
            with self.assertRaisesRegex(ValueError, message):
                replace(claim, **changes)
        error = TaskDagRecoveryClaimError("x" * 2_000, kind="integrity")
        self.assertEqual(error.kind, "integrity")
        self.assertEqual(len(str(error)), 1_000)

    def test_topology_and_ready_selection_are_deterministic(self) -> None:
        dag = TaskDag.create(
            dag_id="diamond",
            parent_session_id="parent",
            nodes=(
                _node("a", 0),
                _node("b", 1, ("a",)),
                _node("c", 2, ("a",)),
                _node("d", 3, ("b", "c")),
                _node("e", 4),
            ),
            created_at=_now(),
        )

        self.assertEqual(dag.topological_order(), ("a", "e", "b", "c", "d"))
        self.assertEqual(dag.ready_node_ids(), ("a", "e"))
        self.assertEqual(dag.node("d").dependencies, ("b", "c"))
        self.assertEqual(dag.definition_fingerprint, dag.definition_fingerprint)

    def test_definition_rejects_cycles_unknown_edges_and_bounds(self) -> None:
        cases = (
            (
                (_node("a", 0, ("missing",)),),
                "unknown dependencies",
            ),
            (
                (_node("a", 0, ("a",)),),
                "depend on itself",
            ),
            (
                (
                    _node("a", 0, ("b",)),
                    _node("b", 1, ("a",)),
                ),
                "contain a cycle",
            ),
        )
        for nodes, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                TaskDag.create(
                    dag_id="invalid",
                    parent_session_id="parent",
                    nodes=nodes,
                    created_at=_now(),
                )

        too_many = tuple(_node(f"node-{index}", index) for index in range(MAX_TASK_DAG_NODES + 1))
        with self.assertRaisesRegex(ValueError, "too many nodes"):
            TaskDag.create(
                dag_id="too-many",
                parent_session_id="parent",
                nodes=too_many,
                created_at=_now(),
            )
        for max_parallel in (0, MAX_TASK_DAG_PARALLELISM + 1, True):
            with (
                self.subTest(max_parallel=max_parallel),
                self.assertRaisesRegex(
                    ValueError,
                    "max_parallel",
                ),
            ):
                TaskDag.create(
                    dag_id="invalid-parallelism",
                    parent_session_id="parent",
                    nodes=(_node("a", 0),),
                    created_at=_now(),
                    max_parallel=max_parallel,
                )

    def test_runtime_snapshot_requires_tuple_nodes_and_running_identity(self) -> None:
        with self.assertRaisesRegex(TypeError, "nodes must be a tuple"):
            TaskDag(
                dag_id="list-nodes",
                parent_session_id="parent",
                nodes=[_node("a", 0)],  # type: ignore[arg-type]
            )
        with self.assertRaisesRegex(ValueError, "requires an exact parent task id"):
            TaskDagNode(
                node_id="running-without-worker",
                ordinal=0,
                prompt="prompt",
                state=TaskDagNodeState.RUNNING,
            )

        with self.assertRaisesRegex(ValueError, "execution owner pid"):
            TaskDagNode(
                node_id="invalid-owner-pid",
                ordinal=0,
                prompt="prompt",
                execution_owner_pid=0,
                execution_owner_token="owner",
            )
        with self.assertRaisesRegex(ValueError, "execution owner identity"):
            TaskDagNode(
                node_id="incomplete-owner",
                ordinal=0,
                prompt="prompt",
                execution_owner_pid=123,
            )

    def test_domain_rejects_untrusted_and_inconsistent_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "safe identifier"):
            TaskDagNode(node_id="bad\x01", ordinal=0, prompt="prompt")
        with self.assertRaisesRegex(ValueError, "non-negative"):
            TaskDagNode(node_id="negative", ordinal=-1, prompt="prompt")
        with self.assertRaisesRegex(ValueError, "non-empty and bounded"):
            TaskDagNode(node_id="empty", ordinal=0, prompt="")
        with self.assertRaisesRegex(ValueError, "non-empty and bounded"):
            TaskDagNode(
                node_id="long-prompt",
                ordinal=0,
                prompt="x" * (MAX_TASK_DAG_PROMPT_BYTES + 1),
            )
        with self.assertRaisesRegex(ValueError, "unsafe control"):
            TaskDagNode(node_id="control", ordinal=0, prompt="bad\x01prompt")
        with self.assertRaisesRegex(ValueError, "error kind is not bounded"):
            TaskDagNode(
                node_id="long-error-kind",
                ordinal=0,
                prompt="prompt",
                error_kind="x" * (MAX_TASK_DAG_ERROR_BYTES + 1),
            )
        with self.assertRaisesRegex(ValueError, "too many dependencies"):
            TaskDagNode(
                node_id="many-deps",
                ordinal=0,
                prompt="prompt",
                dependencies=tuple(
                    f"dep-{index}" for index in range(MAX_TASK_DAG_NODE_DEPENDENCIES + 1)
                ),
            )
        with self.assertRaisesRegex(TypeError, "dependencies must be a tuple"):
            TaskDagNode(
                node_id="list-deps",
                ordinal=0,
                prompt="prompt",
                dependencies=["dep"],  # type: ignore[arg-type]
            )
        with self.assertRaisesRegex(ValueError, "dependencies must be unique"):
            TaskDagNode(
                node_id="duplicate-deps",
                ordinal=0,
                prompt="prompt",
                dependencies=("dep", "dep"),
            )
        with self.assertRaisesRegex(ValueError, "kind must be canonical"):
            TaskDagNode(
                node_id="bad-kind",
                ordinal=0,
                prompt="prompt",
                kind=cast(TaskDagNodeKind, object()),
            )
        with self.assertRaisesRegex(ValueError, "state must be canonical"):
            TaskDagNode(
                node_id="bad-state",
                ordinal=0,
                prompt="prompt",
                state=cast(TaskDagNodeState, object()),
            )
        with self.assertRaisesRegex(ValueError, "generation must be non-negative"):
            TaskDagNode(node_id="bad-generation", ordinal=0, prompt="prompt", generation=True)
        with self.assertRaisesRegex(ValueError, "changed file count"):
            TaskDagNode(node_id="bad-count", ordinal=0, prompt="prompt", changed_file_count=True)
        with self.assertRaisesRegex(ValueError, "unsafe control"):
            TaskDagNode(node_id="bad-error", ordinal=0, prompt="prompt", error_reason="bad\x01")
        with self.assertRaisesRegex(ValueError, "response preview is not bounded"):
            TaskDagNode(
                node_id="long-response",
                ordinal=0,
                prompt="prompt",
                response_preview="x" * (MAX_TASK_DAG_RESPONSE_PREVIEW_BYTES + 1),
            )
        with self.assertRaisesRegex(ValueError, "workspace fingerprint"):
            TaskDagNode(
                node_id="bad-fingerprint",
                ordinal=0,
                prompt="prompt",
                final_workspace_fingerprint="not-a-digest",
            )

        with self.assertRaisesRegex(ValueError, "must not carry execution state"):
            TaskDag.create(
                dag_id="prepopulated",
                parent_session_id="parent",
                nodes=(replace(_node("a", 0), parent_task_id="worker-a"),),
                created_at=_now(),
            )
        with self.assertRaisesRegex(ValueError, "at least one node"):
            TaskDag.create(
                dag_id="empty-dag",
                parent_session_id="parent",
                nodes=(),
                created_at=_now(),
            )
        with self.assertRaisesRegex(ValueError, "node ids must be unique"):
            TaskDag.create(
                dag_id="duplicate-ids",
                parent_session_id="parent",
                nodes=(_node("a", 0), _node("a", 1)),
                created_at=_now(),
            )
        with self.assertRaisesRegex(ValueError, "ordinals must match"):
            TaskDag.create(
                dag_id="bad-ordinals",
                parent_session_id="parent",
                nodes=(_node("a", 0), _node("b", 2)),
                created_at=_now(),
            )
        too_many_edges = tuple(
            _node(
                f"node-{index}",
                index,
                tuple(f"node-{dependency}" for dependency in range(max(0, index - 4), index)),
            )
            for index in range(MAX_TASK_DAG_NODES)
        )
        with self.assertRaisesRegex(ValueError, "too many edges"):
            TaskDag.create(
                dag_id="too-many-edges",
                parent_session_id="parent",
                nodes=too_many_edges,
                created_at=_now(),
            )

        base = TaskDag.create(
            dag_id="invalid-snapshot",
            parent_session_id="parent",
            nodes=(_node("a", 0), _node("b", 1, ("a",))),
            created_at=_now(),
        )
        with self.assertRaisesRegex(ValueError, "state must be canonical"):
            TaskDag(
                dag_id=base.dag_id,
                parent_session_id=base.parent_session_id,
                nodes=base.nodes,
                state=cast(TaskDagState, object()),
            )
        with self.assertRaisesRegex(ValueError, "generation must be non-negative"):
            TaskDag(
                dag_id=base.dag_id,
                parent_session_id=base.parent_session_id,
                nodes=base.nodes,
                generation=-1,
            )
        with self.assertRaisesRegex(ValueError, "creation time must be timezone-aware"):
            TaskDag(
                dag_id=base.dag_id,
                parent_session_id=base.parent_session_id,
                nodes=base.nodes,
                created_at=datetime.min,  # noqa: DTZ901 - intentionally invalid input
            )
        with self.assertRaisesRegex(ValueError, "update time must be timezone-aware"):
            TaskDag(
                dag_id=base.dag_id,
                parent_session_id=base.parent_session_id,
                nodes=base.nodes,
                created_at=_now(),
                updated_at=datetime.min,  # noqa: DTZ901 - intentionally invalid input
            )
        with self.assertRaisesRegex(ValueError, "must not precede creation"):
            TaskDag(
                dag_id=base.dag_id,
                parent_session_id=base.parent_session_id,
                nodes=base.nodes,
                created_at=_now(),
                updated_at=_now() - timedelta(seconds=1),
            )
        with self.assertRaisesRegex(ValueError, "active node must be running"):
            TaskDag(
                dag_id=base.dag_id,
                parent_session_id=base.parent_session_id,
                nodes=base.nodes,
                active_node_id="a",
            )
        running = replace(base.node("a"), state=TaskDagNodeState.RUNNING, parent_task_id="worker-a")
        running_snapshot = TaskDag(
            dag_id=base.dag_id,
            parent_session_id=base.parent_session_id,
            nodes=(running, base.node("b")),
            state=TaskDagState.RUNNING,
        )
        self.assertEqual(running_snapshot.running_node_ids, ("a",))
        with self.assertRaisesRegex(ValueError, "terminal task DAG"):
            replace(running_snapshot, state=TaskDagState.COMPLETED)
        with self.assertRaises(KeyError):
            base.node("missing")
        self.assertEqual(base.with_nodes(base.nodes).node_states(), base.node_states())
        self.assertTrue(TaskDagState.COMPLETED.terminal)
        self.assertTrue(TaskDagNodeState.SKIPPED.terminal)
        self.assertTrue(TaskDagNodeState.COMPLETED.successful)
        self.assertFalse(TaskDagNodeState.FAILED.successful)

        corrupt = TaskDag.create(
            dag_id="corrupt-topology",
            parent_session_id="parent",
            nodes=(_node("a", 0), _node("b", 1, ("a",))),
            created_at=_now(),
        )
        object.__setattr__(corrupt.node("a"), "dependencies", ("b",))
        with self.assertRaisesRegex(ValueError, "contain a cycle"):
            corrupt.topological_order()


class TaskDagPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self._database_path = Path(self._temporary.name) / "sessions.db"
        self.store = SqliteSessionStore(self._database_path)
        await self.store.initialize()
        self.parent_session_id = await self.store.create_session(
            self._temporary.name,
            "fixture",
            "fixture-model",
        )

    async def asyncTearDown(self) -> None:
        self._temporary.cleanup()

    def _dag(self, dag_id: str = "dag") -> TaskDag:
        return TaskDag.create(
            dag_id=dag_id,
            parent_session_id=self.parent_session_id,
            nodes=(_node("a", 0), _node("b", 1, ("a",))),
            created_at=_now(),
        )

    async def test_insert_only_definition_and_generation_cas(self) -> None:
        dag = self._dag()
        self.assertEqual(await self.store.insert_task_dag(dag), dag)
        self.assertEqual(await self.store.insert_task_dag(dag), dag)

        changed = TaskDag.create(
            dag_id=dag.dag_id,
            parent_session_id=self.parent_session_id,
            nodes=(TaskDagNode(node_id="a", ordinal=0, prompt="different"), _node("b", 1, ("a",))),
            created_at=_now(),
        )
        with self.assertRaisesRegex(TaskDagError, "different definition"):
            await self.store.insert_task_dag(changed)
        with self.assertRaisesRegex(TaskDagError, "different max_parallel"):
            await self.store.insert_task_dag(replace(dag, max_parallel=2))

        loaded = await self.store.get_task_dag(dag.dag_id)
        self.assertIsNotNone(loaded)
        assert loaded is not None
        running = replace(
            loaded.node("a"),
            state=TaskDagNodeState.RUNNING,
            generation=1,
            parent_task_id="worker-a",
        )
        claimed = await self.store.claim_task_dag_node(
            dag.dag_id,
            running,
            expected_generation=0,
            expected_state=TaskDagNodeState.READY,
            updated_at=_now() + timedelta(seconds=1),
        )
        with self.assertRaisesRegex(TaskDagError, "capacity|active|changed by another scheduler"):
            await self.store.claim_task_dag_node(
                dag.dag_id,
                running,
                expected_generation=0,
                expected_state=TaskDagNodeState.READY,
                updated_at=_now() + timedelta(seconds=2),
            )

        finished_node = replace(
            claimed.node("a"),
            state=TaskDagNodeState.COMPLETED,
            generation=2,
        )
        finished = await self.store.finish_task_dag_node(
            dag.dag_id,
            finished_node,
            expected_generation=1,
            expected_state=TaskDagNodeState.RUNNING,
            updated_at=_now() + timedelta(seconds=3),
        )
        completed = replace(
            finished,
            state=TaskDagState.COMPLETED,
            generation=finished.generation + 1,
            updated_at=_now() + timedelta(seconds=4),
        )
        transitioned = await self.store.compare_and_transition_task_dag(
            completed,
            expected_generation=finished.generation,
            expected_state=TaskDagState.RUNNING,
        )
        self.assertIs(transitioned.state, TaskDagState.COMPLETED)
        with self.assertRaisesRegex(TaskDagError, "changed by another scheduler"):
            await self.store.compare_and_transition_task_dag(
                completed,
                expected_generation=finished.generation,
                expected_state=TaskDagState.RUNNING,
            )

    async def test_two_process_style_claims_of_different_ready_nodes_have_one_winner(self) -> None:
        dag = TaskDag.create(
            dag_id="two-ready",
            parent_session_id=self.parent_session_id,
            nodes=(_node("a", 0), _node("b", 1)),
            created_at=_now(),
        )
        await self.store.insert_task_dag(dag)
        other_store = SqliteSessionStore(self._database_path)
        await other_store.initialize()

        async def claim(store: SqliteSessionStore, node_id: str) -> object:
            snapshot = await store.get_task_dag(dag.dag_id)
            assert snapshot is not None
            proposed = replace(
                snapshot.node(node_id),
                state=TaskDagNodeState.RUNNING,
                generation=1,
                parent_task_id=f"worker-{node_id}",
            )
            try:
                return await store.claim_task_dag_node(
                    dag.dag_id,
                    proposed,
                    expected_generation=0,
                    expected_state=TaskDagNodeState.READY,
                    updated_at=_now(),
                )
            except TaskDagError as error:
                return error

        results = await asyncio.gather(
            claim(self.store, "a"),
            claim(other_store, "b"),
        )
        winners = [result for result in results if isinstance(result, TaskDag)]
        errors = [result for result in results if isinstance(result, TaskDagError)]
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].kind, "concurrent_modification")
        current = await self.store.get_task_dag(dag.dag_id)
        self.assertIsNotNone(current)
        assert current is not None
        self.assertIsNotNone(current.active_node_id)
        self.assertEqual(
            sum(node.state is TaskDagNodeState.RUNNING for node in current.nodes),
            1,
        )

    async def test_atomic_capacity_claim_allows_two_and_rejects_the_third(self) -> None:
        dag = TaskDag.create(
            dag_id="capacity-two",
            parent_session_id=self.parent_session_id,
            nodes=(_node("a", 0), _node("b", 1), _node("c", 2)),
            created_at=_now(),
            max_parallel=2,
        )
        await self.store.insert_task_dag(dag)
        claimed: list[TaskDag] = []
        for node_id in ("a", "b"):
            snapshot = await self.store.get_task_dag(dag.dag_id)
            assert snapshot is not None
            node = snapshot.node(node_id)
            claimed.append(
                await self.store.claim_task_dag_node(
                    dag.dag_id,
                    replace(
                        node,
                        state=TaskDagNodeState.RUNNING,
                        generation=node.generation + 1,
                        parent_task_id=f"worker-{node_id}",
                    ),
                    expected_generation=node.generation,
                    expected_state=TaskDagNodeState.READY,
                    updated_at=_now(),
                )
            )
        self.assertEqual(len(claimed), 2)
        snapshot = await self.store.get_task_dag(dag.dag_id)
        assert snapshot is not None
        c = snapshot.node("c")
        with self.assertRaisesRegex(TaskDagError, "capacity"):
            await self.store.claim_task_dag_node(
                dag.dag_id,
                replace(
                    c,
                    state=TaskDagNodeState.RUNNING,
                    generation=c.generation + 1,
                    parent_task_id="worker-c",
                ),
                expected_generation=c.generation,
                expected_state=TaskDagNodeState.READY,
                updated_at=_now(),
            )
        self.assertEqual(snapshot.running_node_ids, ("a", "b"))
        self.assertIsNone(snapshot.active_node_id)

        finished_a = replace(
            snapshot.node("a"),
            state=TaskDagNodeState.COMPLETED,
            generation=snapshot.node("a").generation + 1,
        )
        await self.store.finish_task_dag_node(
            dag.dag_id,
            finished_a,
            expected_generation=snapshot.node("a").generation,
            expected_state=TaskDagNodeState.RUNNING,
            updated_at=_now(),
        )
        snapshot = await self.store.get_task_dag(dag.dag_id)
        assert snapshot is not None
        c = snapshot.node("c")
        after_release = await self.store.claim_task_dag_node(
            dag.dag_id,
            replace(
                c,
                state=TaskDagNodeState.RUNNING,
                generation=c.generation + 1,
                parent_task_id="worker-c",
            ),
            expected_generation=c.generation,
            expected_state=TaskDagNodeState.READY,
            updated_at=_now(),
        )
        self.assertEqual(after_release.running_node_ids, ("b", "c"))

    async def test_generic_node_transition_cannot_bypass_capacity_claim(self) -> None:
        dag = TaskDag.create(
            dag_id="capacity-transition-guard",
            parent_session_id=self.parent_session_id,
            nodes=(_node("a", 0),),
            created_at=_now(),
        )
        await self.store.insert_task_dag(dag)
        snapshot = await self.store.get_task_dag(dag.dag_id)
        assert snapshot is not None
        node = snapshot.node("a")
        with self.assertRaisesRegex(TaskDagError, "atomic capacity claim"):
            await self.store.compare_and_transition_task_dag_node(
                dag.dag_id,
                replace(
                    node,
                    state=TaskDagNodeState.RUNNING,
                    generation=node.generation + 1,
                    parent_task_id="worker-a",
                ),
                expected_generation=node.generation,
                expected_state=TaskDagNodeState.READY,
            )

    async def test_spawned_controllers_never_exceed_durable_capacity(self) -> None:
        dag = TaskDag.create(
            dag_id="spawn-capacity",
            parent_session_id=self.parent_session_id,
            nodes=(_node("a", 0), _node("b", 1), _node("c", 2)),
            created_at=_now(),
            max_parallel=2,
        )
        await self.store.insert_task_dag(dag)
        context = multiprocessing.get_context("spawn")
        barrier = context.Barrier(3)
        queue = context.Queue()
        processes = [
            context.Process(
                target=_spawn_parallel_claim_worker,
                args=(str(self._database_path), dag.dag_id, node_id, barrier, queue),
            )
            for node_id in ("a", "b", "c")
        ]
        for process in processes:
            process.start()
        results = [queue.get(timeout=30) for _ in processes]
        for process in processes:
            process.join(timeout=30)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
            self.assertEqual(process.exitcode, 0)
        self.assertEqual(results.count("claimed"), 2)
        self.assertEqual(sum(result.startswith("error:") for result in results), 1)
        current = await self.store.get_task_dag(dag.dag_id)
        self.assertIsNotNone(current)
        assert current is not None
        self.assertLessEqual(len(current.running_node_ids), 2)
        self.assertEqual(len(current.running_node_ids), 2)

    async def test_schema_17_migrates_to_26_and_creates_dag_leader_relay_recovery_planning_and_replan_tables(
        self,
    ) -> None:
        connection = sqlite3.connect(self._database_path)
        connection.execute("UPDATE schema_meta SET version = 17 WHERE singleton = 1")
        connection.commit()
        connection.close()

        reopened = SqliteSessionStore(self._database_path)
        await reopened.initialize()
        connection = sqlite3.connect(self._database_path)
        version = connection.execute(
            "SELECT version FROM schema_meta WHERE singleton = 1"
        ).fetchone()
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        connection.close()
        self.assertEqual(version, (SCHEMA_VERSION,))
        self.assertTrue({"task_dags", "task_dag_nodes"}.issubset(tables))
        self.assertTrue({"leader_attempts", "leader_decisions"}.issubset(tables))
        self.assertIn("task_dag_dependency_relays", tables)
        self.assertIn("task_dag_recovery_claims", tables)
        self.assertTrue(
            {
                "orchestration_planning_attempts",
                "orchestration_plan_proposals",
            }.issubset(tables)
        )
        self.assertTrue(
            {
                "orchestration_dag_replan_attempts",
                "orchestration_dag_replan_proposals",
            }.issubset(tables)
        )
        self.assertIsNotNone(await reopened.get_session(self.parent_session_id))

    async def test_populated_schema_18_dag_survives_leader_migration(self) -> None:
        dag = TaskDag.create(
            dag_id="populated-schema-18",
            parent_session_id=self.parent_session_id,
            nodes=(_node("a", 0),),
            created_at=_now(),
        )
        await self.store.insert_task_dag(dag)
        connection = sqlite3.connect(self._database_path)
        connection.execute("DROP TABLE leader_decisions")
        connection.execute("DROP TABLE leader_attempts")
        connection.execute("UPDATE schema_meta SET version = 18 WHERE singleton = 1")
        connection.commit()
        connection.close()

        reopened = SqliteSessionStore(self._database_path)
        await reopened.initialize()
        preserved = await reopened.get_task_dag(dag.dag_id)
        self.assertIsNotNone(preserved)
        assert preserved is not None
        self.assertEqual(preserved.definition_fingerprint, dag.definition_fingerprint)
        self.assertEqual(preserved.node("a").prompt, "prompt-a")
        connection = sqlite3.connect(self._database_path)
        version = connection.execute(
            "SELECT version FROM schema_meta WHERE singleton = 1"
        ).fetchone()
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        connection.close()
        self.assertEqual(version, (SCHEMA_VERSION,))
        self.assertTrue({"leader_attempts", "leader_decisions"}.issubset(tables))
        self.assertIn("task_dag_recovery_claims", tables)

    async def test_populated_schema_23_leader_decision_migrates_to_parallel_projection(
        self,
    ) -> None:
        dag = self._dag("populated-schema-23-leader")
        await self.store.insert_task_dag(dag)
        leader_session_id = await self.store.create_session(
            self._temporary.name,
            "fixture-leader",
            "fixture-model",
        )
        created_at = _now().isoformat()
        connection = sqlite3.connect(self._database_path)
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DROP TABLE leader_decisions")
        connection.execute("DROP TABLE leader_attempts")
        connection.execute(
            """
            CREATE TABLE leader_attempts (
                attempt_id TEXT PRIMARY KEY,
                dag_id TEXT NOT NULL,
                leader_session_id TEXT NOT NULL,
                objective_fingerprint TEXT NOT NULL,
                dag_generation INTEGER NOT NULL CHECK (dag_generation >= 0),
                definition_fingerprint TEXT NOT NULL,
                evidence_fingerprint TEXT NOT NULL,
                state TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                lease_expires_at TEXT NOT NULL,
                turn_id TEXT NOT NULL UNIQUE,
                model_response TEXT,
                decision_id TEXT UNIQUE,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(
                    dag_id,
                    dag_generation,
                    definition_fingerprint,
                    evidence_fingerprint,
                    objective_fingerprint
                ),
                FOREIGN KEY (dag_id) REFERENCES task_dags(dag_id) ON DELETE RESTRICT,
                FOREIGN KEY (leader_session_id) REFERENCES sessions(id) ON DELETE RESTRICT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE leader_decisions (
                decision_id TEXT PRIMARY KEY,
                attempt_id TEXT NOT NULL UNIQUE,
                dag_id TEXT NOT NULL,
                leader_session_id TEXT NOT NULL,
                dag_generation INTEGER NOT NULL CHECK (dag_generation >= 0),
                definition_fingerprint TEXT NOT NULL,
                evidence_fingerprint TEXT NOT NULL,
                kind TEXT NOT NULL CHECK (kind IN ('SELECT_NODE', 'FINALIZE')),
                selected_node_id TEXT,
                summary TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (attempt_id) REFERENCES leader_attempts(attempt_id) ON DELETE RESTRICT,
                FOREIGN KEY (dag_id) REFERENCES task_dags(dag_id) ON DELETE RESTRICT,
                FOREIGN KEY (leader_session_id) REFERENCES sessions(id) ON DELETE RESTRICT
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX leader_attempts_by_dag
            ON leader_attempts(dag_id, dag_generation, created_at, attempt_id)
            """
        )
        connection.execute(
            """
            CREATE INDEX leader_decisions_by_dag
            ON leader_decisions(dag_id, created_at, decision_id)
            """
        )
        connection.execute(
            """
            INSERT INTO leader_attempts(
                attempt_id, dag_id, leader_session_id, objective_fingerprint,
                dag_generation, definition_fingerprint, evidence_fingerprint,
                state, owner_id, lease_expires_at, turn_id, model_response,
                decision_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-leader-attempt",
                dag.dag_id,
                leader_session_id,
                "a" * 64,
                dag.generation,
                dag.definition_fingerprint,
                "b" * 64,
                "decision_published",
                "legacy-owner",
                created_at,
                "legacy-leader-turn",
                '{"action":"SELECT_NODE","node_id":"a"}',
                "legacy-leader-decision",
                created_at,
                created_at,
            ),
        )
        connection.execute(
            """
            INSERT INTO leader_decisions(
                decision_id, attempt_id, dag_id, leader_session_id,
                dag_generation, definition_fingerprint, evidence_fingerprint,
                kind, selected_node_id, summary, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-leader-decision",
                "legacy-leader-attempt",
                dag.dag_id,
                leader_session_id,
                dag.generation,
                dag.definition_fingerprint,
                "b" * 64,
                "SELECT_NODE",
                "a",
                "legacy",
                created_at,
            ),
        )
        connection.execute("UPDATE schema_meta SET version = 23 WHERE singleton = 1")
        connection.commit()
        connection.close()

        reopened = SqliteSessionStore(self._database_path)
        await reopened.initialize()
        attempt = await reopened.get_leader_attempt("legacy-leader-attempt")
        decision = await reopened.get_leader_decision("legacy-leader-decision")
        self.assertIsNotNone(attempt)
        self.assertIsNotNone(decision)
        assert attempt is not None
        assert decision is not None
        self.assertEqual(attempt.parent_session_id, self.parent_session_id)
        self.assertEqual(decision.parent_session_id, self.parent_session_id)
        self.assertEqual(decision.decision.selected_node_ids, ("a",))
        self.assertEqual(decision.selected_node_generations, ())
        connection = sqlite3.connect(self._database_path)
        version = connection.execute(
            "SELECT version FROM schema_meta WHERE singleton = 1"
        ).fetchone()
        decision_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'leader_decisions'"
        ).fetchone()
        connection.close()
        self.assertEqual(version, (SCHEMA_VERSION,))
        self.assertIn("SELECT_NODES", str(decision_sql[0]))

    async def test_populated_schema_20_migrates_to_26_without_touching_dag_contract(self) -> None:
        dag = self._dag("populated-schema-20")
        await self.store.insert_task_dag(dag)
        connection = sqlite3.connect(self._database_path)
        connection.execute("DROP TABLE task_dag_recovery_claims")
        connection.execute("UPDATE schema_meta SET version = 20 WHERE singleton = 1")
        connection.commit()
        connection.close()

        reopened = SqliteSessionStore(self._database_path)
        await reopened.initialize()
        preserved = await reopened.get_task_dag(dag.dag_id)
        self.assertEqual(preserved, dag)
        connection = sqlite3.connect(self._database_path)
        version = connection.execute(
            "SELECT version FROM schema_meta WHERE singleton = 1"
        ).fetchone()
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        indexes = {
            row[1]
            for row in connection.execute("PRAGMA index_list(task_dag_recovery_claims)").fetchall()
        }
        foreign_keys = connection.execute(
            "PRAGMA foreign_key_list(task_dag_recovery_claims)"
        ).fetchall()
        connection.close()
        self.assertEqual(version, (SCHEMA_VERSION,))
        self.assertTrue(
            {
                "task_dags",
                "task_dag_nodes",
                "task_dag_dependency_relays",
                "leader_attempts",
                "leader_decisions",
                "writable_subagent_leases",
                "parent_context_relays",
                "task_dag_recovery_claims",
            }.issubset(tables)
        )
        self.assertIn("task_dag_recovery_claims_by_execution", indexes)
        self.assertEqual({row[6] for row in foreign_keys}, {"RESTRICT"})

        running = replace(
            dag.node("a"),
            state=TaskDagNodeState.RUNNING,
            generation=1,
            parent_task_id="migration-worker-a",
        )
        claimed = await reopened.claim_task_dag_node(
            dag.dag_id,
            running,
            expected_generation=0,
            expected_state=TaskDagNodeState.READY,
            updated_at=_now(),
        )
        self.assertEqual(claimed.node("a").parent_task_id, "migration-worker-a")

    async def test_populated_schema_21_running_relay_and_recovery_claim_survive_schema_26(
        self,
    ) -> None:
        dag = self._dag("populated-schema-21")
        await self.store.insert_task_dag(dag)
        snapshot = await self.store.get_task_dag(dag.dag_id)
        assert snapshot is not None

        running_a = replace(
            snapshot.node("a"),
            state=TaskDagNodeState.RUNNING,
            generation=1,
            parent_task_id="migration-worker-a",
        )
        snapshot = await self.store.claim_task_dag_node(
            dag.dag_id,
            running_a,
            expected_generation=0,
            expected_state=TaskDagNodeState.READY,
            updated_at=_now(),
        )
        finished_a = replace(
            snapshot.node("a"),
            state=TaskDagNodeState.COMPLETED,
            generation=2,
        )
        snapshot = await self.store.finish_task_dag_node(
            dag.dag_id,
            finished_a,
            expected_generation=1,
            expected_state=TaskDagNodeState.RUNNING,
            updated_at=_now(),
        )
        ready_b = replace(
            snapshot.node("b"),
            state=TaskDagNodeState.READY,
            generation=snapshot.node("b").generation + 1,
        )
        snapshot = await self.store.compare_and_transition_task_dag_node(
            dag.dag_id,
            ready_b,
            expected_generation=snapshot.node("b").generation,
            expected_state=TaskDagNodeState.PENDING,
        )
        running_b = replace(
            snapshot.node("b"),
            state=TaskDagNodeState.RUNNING,
            generation=snapshot.node("b").generation + 1,
            parent_task_id="migration-worker-b",
        )
        snapshot = await self.store.claim_task_dag_node(
            dag.dag_id,
            running_b,
            expected_generation=ready_b.generation,
            expected_state=TaskDagNodeState.READY,
            updated_at=_now(),
        )

        digest = "e" * 64
        created_at = _now().isoformat()
        connection = sqlite3.connect(self._database_path)
        connection.execute(
            """
            INSERT INTO task_dag_dependency_relays(
                relay_id, dag_id, dag_definition_fingerprint, target_node_id,
                target_node_generation, target_node_definition_fingerprint,
                direct_dependency_ids_json, entries_json, source_fingerprint,
                content_fingerprint, byte_count, truncated, created_at,
                integrity_fingerprint, state
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ready')
            """,
            (
                "migration-relay",
                dag.dag_id,
                dag.definition_fingerprint,
                "b",
                snapshot.node("b").generation,
                snapshot.node("b").definition_fingerprint,
                '["a"]',
                "[]",
                digest,
                digest,
                1,
                0,
                created_at,
                digest,
            ),
        )
        connection.execute(
            """
            INSERT INTO task_dag_recovery_claims(
                claim_id, parent_session_id, dag_id, dag_definition_fingerprint,
                node_id, node_generation, node_definition_fingerprint,
                parent_task_id, dependency_relay_id,
                dependency_relay_source_fingerprint,
                dependency_relay_content_fingerprint,
                dependency_relay_integrity_fingerprint, owner_pid, owner_token,
                version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "migration-claim",
                self.parent_session_id,
                dag.dag_id,
                dag.definition_fingerprint,
                "b",
                snapshot.node("b").generation,
                snapshot.node("b").definition_fingerprint,
                "migration-worker-b",
                "migration-relay",
                digest,
                digest,
                digest,
                999_999,
                "migration-owner",
                0,
                created_at,
                created_at,
            ),
        )
        connection.execute("ALTER TABLE task_dags DROP COLUMN max_parallel")
        connection.execute("UPDATE schema_meta SET version = 21 WHERE singleton = 1")
        connection.commit()
        connection.close()

        reopened = SqliteSessionStore(self._database_path)
        await reopened.initialize()
        preserved = await reopened.get_task_dag(dag.dag_id)
        self.assertIsNotNone(preserved)
        assert preserved is not None
        self.assertEqual(preserved.max_parallel, 1)
        self.assertEqual(preserved.running_node_ids, ("b",))
        self.assertEqual(preserved.active_node_id, "b")
        self.assertEqual(preserved.node("b").generation, snapshot.node("b").generation)
        connection = sqlite3.connect(self._database_path)
        version = connection.execute(
            "SELECT version FROM schema_meta WHERE singleton = 1"
        ).fetchone()
        relay_count = connection.execute(
            "SELECT COUNT(*) FROM task_dag_dependency_relays WHERE relay_id = 'migration-relay'"
        ).fetchone()
        claim_count = connection.execute(
            "SELECT COUNT(*) FROM task_dag_recovery_claims WHERE claim_id = 'migration-claim'"
        ).fetchone()
        connection.close()
        self.assertEqual(version, (SCHEMA_VERSION,))
        self.assertEqual(relay_count, (1,))
        self.assertEqual(claim_count, (1,))

    async def test_recovery_claim_store_rejects_noncanonical_arguments(self) -> None:
        with self.assertRaises(TypeError):
            await self.store.insert_task_dag_recovery_claim(cast(TaskDagRecoveryClaim, object()))
        for generation in (-1, True):
            with self.assertRaises(ValueError):
                await self.store.get_task_dag_recovery_claim("dag", "node", generation)

        digest = "b" * 64
        claim = TaskDagRecoveryClaim.create(
            parent_session_id=self.parent_session_id,
            dag_id="dag",
            dag_definition_fingerprint=digest,
            node_id="node",
            node_generation=1,
            node_definition_fingerprint=digest,
            parent_task_id="task",
            dependency_relay_id="relay",
            dependency_relay_source_fingerprint=digest,
            dependency_relay_content_fingerprint=digest,
            dependency_relay_integrity_fingerprint=digest,
            owner_pid=123,
            owner_token="owner",
            created_at=_now(),
        )
        with self.assertRaises(TypeError):
            await self.store.compare_and_takeover_task_dag_recovery_claim(
                cast(TaskDagRecoveryClaim, object()),
                expected_version=0,
                expected_owner_pid=123,
                expected_owner_token="owner",
            )
        with self.assertRaises(TypeError):
            await self.store.compare_and_takeover_task_dag_recovery_claim(
                claim,
                expected_version=True,
                expected_owner_pid=123,
                expected_owner_token="owner",
            )
        with self.assertRaises(TypeError):
            await self.store.compare_and_takeover_task_dag_recovery_claim(
                claim,
                expected_version=0,
                expected_owner_pid=True,
                expected_owner_token="owner",
            )
        with self.assertRaises(TypeError):
            await self.store.compare_and_takeover_task_dag_recovery_claim(
                claim,
                expected_version=0,
                expected_owner_pid=123,
                expected_owner_token="",
            )


class TaskDagSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.store = SqliteSessionStore(Path(self._temporary.name) / "sessions.db")
        await self.store.initialize()
        self.parent_session_id = await self.store.create_session(
            self._temporary.name,
            "fixture",
            "fixture-model",
        )
        self.binding = _binding(self.parent_session_id)
        self.leases = _FakeLeaseStore()
        self.relays = _FakeRelayStore()

    async def asyncTearDown(self) -> None:
        self._temporary.cleanup()

    def _service(self, writable: _FakeWritableService) -> TaskDagApplicationService:
        return TaskDagApplicationService(
            self.store,
            self.store,
            writable,
            self.leases,
            self.relays,
            parent_binding=self.binding,
        )

    def _evidence(self, parent_task_id: str, node_id: str) -> object:
        return SimpleNamespace(
            parent_session_id=self.parent_session_id,
            parent_task_id=parent_task_id,
            child_session_id=f"child-{node_id}",
            lease_id=f"lease-{node_id}",
            worktree_id=WorktreeId(f"worktree-{node_id}"),
            baseline_checkpoint_id=None,
            final_workspace_fingerprint=None,
            changed_file_count=0,
            state=WritableSubagentWorkspaceState.PRESERVED,
        )

    async def test_service_rejects_noncanonical_requests_and_binding_errors(self) -> None:
        writable = _FakeWritableService(self.parent_session_id)
        service = self._service(writable)
        with self.assertRaisesRegex(ValueError, "creation request must be canonical"):
            await service.create_task_dag(cast(CreateTaskDagRequest, object()))
        with self.assertRaisesRegex(ValueError, "query request must be canonical"):
            await service.get_task_dag(cast(RunTaskDagRequest, object()))
        with self.assertRaisesRegex(ValueError, "run request must be canonical"):
            await service.run_task_dag(cast(RunTaskDagRequest, object()))
        with self.assertRaisesRegex(ValueError, "reconciliation request must be canonical"):
            await service.reconcile_task_dag(cast(RunTaskDagRequest, object()))
        with self.assertRaisesRegex(ConfigurationError, "unknown task DAG"):
            await service.run_task_dag(RunTaskDagRequest("unknown"))

        with self.assertRaisesRegex(ConfigurationError, "parent binding is required"):
            TaskDagApplicationService(
                self.store,
                self.store,
                writable,
                self.leases,
                self.relays,
                parent_binding=cast(ConversationBinding, object()),
            )

    def test_scheduler_request_boundaries_are_validated(self) -> None:
        with self.assertRaisesRegex(TypeError, "nodes must be a tuple"):
            CreateTaskDagRequest("invalid", [_node("a", 0)])  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "max_parallel must be between"):
            CreateTaskDagRequest("invalid", (_node("a", 0),), max_parallel=0)
        with self.assertRaisesRegex(ValueError, "request id must not be empty"):
            RunTaskDagRequest("")
        with self.assertRaisesRegex(ValueError, "step request id must not be empty"):
            RunTaskDagStepRequest("")
        with self.assertRaisesRegex(ValueError, "selected node id must not be empty"):
            RunTaskDagStepRequest("dag", "")
        with self.assertRaisesRegex(ValueError, "wave selected node ids"):
            RunTaskDagWaveRequest("dag", ())
        with self.assertRaisesRegex(ValueError, "wave request id"):
            RunTaskDagWaveRequest("", ("a",))
        with self.assertRaisesRegex(ValueError, "non-empty tuple"):
            RunTaskDagWaveRequest("dag", ["a"])  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "safe text"):
            RunTaskDagWaveRequest("dag", ("",))
        with self.assertRaisesRegex(ValueError, "unique"):
            RunTaskDagWaveRequest("dag", ("a", "a"))
        with self.assertRaisesRegex(ValueError, "graph generation"):
            RunTaskDagWaveRequest("dag", ("a",), expected_dag_generation=True)
        with self.assertRaisesRegex(ValueError, "graph generation"):
            RunTaskDagWaveRequest("dag", ("a",), expected_dag_generation=-1)
        with self.assertRaisesRegex(TypeError, "node generations must be a tuple"):
            RunTaskDagWaveRequest("dag", ("a",), expected_node_generations=[("a", 0)])  # type: ignore[arg-type]
        for generations in (
            (("a",),),
            ((1, 0),),
            (("", 0),),
            (("a", True),),
            (("a", "0"),),
            (("a", -1),),
        ):
            with self.assertRaisesRegex(ValueError, "expected node generation"):
                RunTaskDagWaveRequest("dag", ("a",), expected_node_generations=generations)
        with self.assertRaisesRegex(ValueError, "expected generations"):
            RunTaskDagWaveRequest("dag", ("a",), expected_node_generations=(("b", 0),))

    async def test_leader_wave_rejects_stale_and_unsupported_requests(self) -> None:
        writable = _FakeWritableService(self.parent_session_id)
        service = self._service(writable)
        await service.create_task_dag(
            CreateTaskDagRequest("wave-boundary", (_node("a", 0), _node("b", 1)), 2)
        )
        with self.assertRaisesRegex(ValueError, "wave request must be canonical"):
            await service.run_task_dag_wave(cast(RunTaskDagWaveRequest, object()))
        with self.assertRaisesRegex(ConfigurationError, "graph generation"):
            await service.run_task_dag_wave(
                RunTaskDagWaveRequest(
                    "wave-boundary",
                    ("a",),
                    expected_dag_generation=99,
                    expected_node_generations=(("a", 0),),
                )
            )
        with self.assertRaisesRegex(ConfigurationError, "node generation"):
            await service.run_task_dag_wave(
                RunTaskDagWaveRequest(
                    "wave-boundary",
                    ("a",),
                    expected_dag_generation=0,
                    expected_node_generations=(("a", 1),),
                )
            )
        with self.assertRaisesRegex(ConfigurationError, "independent writable worker factory"):
            await service.run_task_dag_wave(
                RunTaskDagWaveRequest(
                    "wave-boundary",
                    ("a",),
                    expected_dag_generation=0,
                    expected_node_generations=(("a", 0),),
                )
            )

        await service.create_task_dag(CreateTaskDagRequest("wave-terminal", (_node("a", 0),)))
        await service.run_task_dag(RunTaskDagRequest("wave-terminal"))
        terminal = await self.store.get_task_dag("wave-terminal")
        self.assertIsNotNone(terminal)
        assert terminal is not None
        self.assertTrue(terminal.state.terminal)
        with self.assertRaisesRegex(ConfigurationError, "terminal"):
            await service.run_task_dag_wave(
                RunTaskDagWaveRequest(
                    "wave-terminal",
                    ("a",),
                    expected_dag_generation=terminal.generation,
                    expected_node_generations=(("a", terminal.node("a").generation),),
                )
            )

        applied = "wave-applied"
        await service.create_task_dag(
            CreateTaskDagRequest(applied, (_node("a", 0), _node("b", 1)), 2)
        )
        snapshot = await self.store.get_task_dag(applied)
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        running = replace(
            snapshot.node("a"),
            state=TaskDagNodeState.RUNNING,
            generation=1,
            parent_task_id="worker-a",
        )
        claimed = await self.store.claim_task_dag_node(
            applied,
            running,
            expected_generation=0,
            expected_state=TaskDagNodeState.READY,
            updated_at=_now(),
        )
        await self.store.finish_task_dag_node(
            applied,
            replace(claimed.node("a"), state=TaskDagNodeState.COMPLETED, generation=2),
            expected_generation=1,
            expected_state=TaskDagNodeState.RUNNING,
            updated_at=_now(),
        )
        applied_snapshot = await self.store.get_task_dag(applied)
        self.assertIsNotNone(applied_snapshot)
        assert applied_snapshot is not None
        applied_result = await service.run_task_dag_wave(
            RunTaskDagWaveRequest(
                applied,
                ("a",),
                expected_dag_generation=applied_snapshot.generation,
                expected_node_generations=(("a", 0),),
            )
        )
        self.assertIs(applied_result.node("a").state, TaskDagNodeState.COMPLETED)
        with self.assertRaisesRegex(ConfigurationError, "independent writable worker factory"):
            service._worker_service_for(applied_snapshot)

        class SharedFactory:
            def create(self) -> _FakeWritableService:
                return writable

        shared_service = TaskDagApplicationService(
            self.store,
            self.store,
            writable,
            self.leases,
            self.relays,
            parent_binding=self.binding,
            writable_worker_factory=SharedFactory(),
        )
        await shared_service.create_task_dag(
            CreateTaskDagRequest("wave-shared", (_node("a", 0), _node("b", 1)), 2)
        )
        with self.assertRaisesRegex(ConfigurationError, "shared writable service"):
            await shared_service.run_task_dag_wave(
                RunTaskDagWaveRequest(
                    "wave-shared",
                    ("a", "b"),
                    expected_dag_generation=0,
                    expected_node_generations=(("a", 0), ("b", 0)),
                )
            )

        race_state = _ParallelRunState(set())
        race_service, _ = self._parallel_service(race_state)
        await race_service.create_task_dag(
            CreateTaskDagRequest("wave-race", (_node("a", 0), _node("b", 1)), 2)
        )
        delegate = self.store

        class AlwaysRaceStore:
            def __getattr__(self, name: str) -> object:
                return getattr(delegate, name)

            async def claim_task_dag_node(self, *args, **kwargs):
                del args, kwargs
                raise TaskDagError("claim race", kind="concurrent_modification")

        race_service._dag_store = AlwaysRaceStore()  # type: ignore[assignment]
        race_result = await race_service.run_task_dag_wave(
            RunTaskDagWaveRequest(
                "wave-race",
                ("a", "b"),
                expected_dag_generation=0,
                expected_node_generations=(("a", 0), ("b", 0)),
            )
        )
        self.assertEqual(
            tuple(node.state for node in race_result.nodes),
            (TaskDagNodeState.READY, TaskDagNodeState.READY),
        )

    async def test_parallel_scheduler_requires_independent_worker_factory(self) -> None:
        writable = _FakeWritableService(self.parent_session_id)
        service = self._service(writable)
        await service.create_task_dag(
            CreateTaskDagRequest("missing-factory", (_node("a", 0), _node("b", 1)), 2)
        )
        with self.assertRaisesRegex(ConfigurationError, "independent writable worker factory"):
            await service.run_task_dag(RunTaskDagRequest("missing-factory"))

        class SharedFactory:
            def __init__(self, worker: _FakeWritableService) -> None:
                self.worker = worker

            def create(self) -> _FakeWritableService:
                return self.worker

        shared_service = TaskDagApplicationService(
            self.store,
            self.store,
            writable,
            self.leases,
            self.relays,
            parent_binding=self.binding,
            writable_worker_factory=SharedFactory(writable),
        )
        await shared_service.create_task_dag(
            CreateTaskDagRequest("shared-factory", (_node("a", 0), _node("b", 1)), 2)
        )
        with self.assertRaisesRegex(ConfigurationError, "shared writable service"):
            await shared_service.run_task_dag(RunTaskDagRequest("shared-factory"))

        class InvalidFactory:
            def create(self) -> object:
                return object()

        invalid_service = TaskDagApplicationService(
            self.store,
            self.store,
            writable,
            self.leases,
            self.relays,
            parent_binding=self.binding,
            writable_worker_factory=InvalidFactory(),
        )
        invalid_dag = TaskDag.create(
            dag_id="invalid-factory",
            parent_session_id=self.parent_session_id,
            nodes=(_node("a", 0),),
            created_at=_now(),
            max_parallel=2,
        )
        with self.assertRaisesRegex(ConfigurationError, "invalid service"):
            invalid_service._worker_service_for(invalid_dag)

        wrong_parent_service = TaskDagApplicationService(
            self.store,
            self.store,
            writable,
            self.leases,
            self.relays,
            parent_binding=self.binding,
            writable_worker_factory=SharedFactory(_FakeWritableService("other-parent")),
        )
        with self.assertRaisesRegex(ConfigurationError, "does not match binding"):
            wrong_parent_service._worker_service_for(invalid_dag)

        with self.assertRaisesRegex(ConfigurationError, "worker factory is invalid"):
            TaskDagApplicationService(
                self.store,
                self.store,
                writable,
                self.leases,
                self.relays,
                parent_binding=self.binding,
                writable_worker_factory=cast(object, object()),
            )

    async def test_terminal_dag_rejects_selected_step(self) -> None:
        service = self._service(_FakeWritableService(self.parent_session_id))
        await service.create_task_dag(CreateTaskDagRequest("terminal-step", (_node("a", 0),)))
        terminal = await service.run_task_dag(RunTaskDagRequest("terminal-step"))
        self.assertTrue(terminal.state.terminal)
        self.assertEqual(
            await service.run_task_dag_step(RunTaskDagStepRequest("terminal-step")),
            terminal,
        )
        with self.assertRaisesRegex(ConfigurationError, "terminal task DAG"):
            await service.run_task_dag_step(RunTaskDagStepRequest("terminal-step", "a"))

    async def test_serial_step_observes_live_worker_before_selected_node_validation(self) -> None:
        service = self._service(_FakeWritableService(self.parent_session_id))
        await service.create_task_dag(CreateTaskDagRequest("live-step", (_node("a", 0),)))
        snapshot = await self.store.get_task_dag("live-step")
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        await self.store.claim_task_dag_node(
            "live-step",
            replace(
                snapshot.node("a"),
                state=TaskDagNodeState.RUNNING,
                generation=1,
                parent_task_id="live-worker-a",
                execution_owner_pid=os.getpid(),
                execution_owner_token="live-worker-owner",
            ),
            expected_generation=0,
            expected_state=TaskDagNodeState.READY,
            updated_at=_now(),
        )
        observed = await service.run_task_dag_step(RunTaskDagStepRequest("live-step"))
        self.assertIs(observed.node("a").state, TaskDagNodeState.RUNNING)
        with self.assertRaisesRegex(ConfigurationError, "already has a running node"):
            await service.run_task_dag_step(RunTaskDagStepRequest("live-step", "a"))

    async def test_serial_step_claim_races_are_bounded(self) -> None:
        for error, message in (
            (
                TaskDagError("claim race", kind="concurrent_modification"),
                "selected task DAG node became stale",
            ),
            (
                TaskDagError("claim failed", kind="command_failed"),
                "task DAG node claim failed",
            ),
        ):
            with self.subTest(message=message):
                dag_store = _IntermittentClaimStore(self.store, error)
                service = TaskDagApplicationService(
                    self.store,
                    dag_store,
                    _FakeWritableService(self.parent_session_id),
                    self.leases,
                    self.relays,
                    parent_binding=self.binding,
                )
                dag_id = f"step-claim-{error.kind}"
                await service.create_task_dag(CreateTaskDagRequest(dag_id, (_node("a", 0),)))
                with self.assertRaisesRegex(ConfigurationError, message):
                    await service.run_task_dag_step(RunTaskDagStepRequest(dag_id, "a"))

    async def test_recovery_without_ownership_boundary_fails_closed(self) -> None:
        service = self._service(_FakeWritableService(self.parent_session_id))
        dag = await service.create_task_dag(
            CreateTaskDagRequest("recovery-without-boundary", (_node("a", 0),))
        )
        running = replace(
            dag.node("a"),
            state=TaskDagNodeState.RUNNING,
            generation=1,
            parent_task_id="recovery-worker-a",
        )
        claimed = await self.store.claim_task_dag_node(
            dag.dag_id,
            running,
            expected_generation=0,
            expected_state=TaskDagNodeState.READY,
            updated_at=_now(),
        )
        self.assertFalse(await service._acquire_recovery_ownership(claimed, claimed.node("a")))
        current = await self.store.get_task_dag(dag.dag_id)
        self.assertIsNotNone(current)
        assert current is not None
        self.assertIs(current.node("a").state, TaskDagNodeState.INDETERMINATE)
        with self.assertRaisesRegex(ConfigurationError, "writable service is invalid"):
            TaskDagApplicationService(
                self.store,
                self.store,
                cast(TaskDagWritableService, object()),
                self.leases,
                self.relays,
                parent_binding=self.binding,
            )
        with self.assertRaisesRegex(ConfigurationError, "session identity is missing"):
            TaskDagApplicationService(
                self.store,
                self.store,
                _FakeWritableService(""),
                self.leases,
                self.relays,
                parent_binding=_binding(""),
            )

    async def test_one_step_executes_only_the_selected_current_ready_node(self) -> None:
        writable = _FakeWritableService(self.parent_session_id)
        original_run = writable.run_subagent_with_execution_identity

        async def run_and_publish(request, *, execution_identity, sink=None):
            self.leases.by_parent_task[execution_identity.parent_task_id] = self._evidence(
                execution_identity.parent_task_id,
                execution_identity.node_id,
            )
            return await original_run(
                request,
                execution_identity=execution_identity,
                sink=sink,
            )

        writable.run_subagent_with_execution_identity = run_and_publish
        service = self._service(writable)
        await service.create_task_dag(
            CreateTaskDagRequest(
                "one-step",
                (_node("a", 0), _node("b", 1), _node("c", 2, ("a",))),
            )
        )
        prepared = await service.prepare_task_dag_step(RunTaskDagRequest("one-step"))
        self.assertEqual(prepared.ready_node_ids(), ("a", "b"))
        self.assertEqual(writable.calls, [])

        after_b = await service.run_task_dag_step(RunTaskDagStepRequest("one-step", "b"))
        self.assertEqual([call[1] for call in writable.calls], ["b"])
        self.assertIs(after_b.node("b").state, TaskDagNodeState.COMPLETED)
        self.assertIs(after_b.node("a").state, TaskDagNodeState.READY)
        self.assertIs(after_b.node("c").state, TaskDagNodeState.PENDING)
        with self.assertRaisesRegex(ConfigurationError, "not currently READY"):
            await service.run_task_dag_step(RunTaskDagStepRequest("one-step", "c"))

    async def test_publication_and_parent_verification_fail_closed(self) -> None:
        foreign = TaskDag.create(
            dag_id="foreign",
            parent_session_id="different-parent",
            nodes=(_node("a", 0),),
            created_at=_now(),
        )
        dag_store = SimpleNamespace(
            insert_task_dag=AsyncMock(
                side_effect=TaskDagError("definition conflict", kind="definition_conflict")
            ),
            get_task_dag=AsyncMock(return_value=foreign),
        )
        service = TaskDagApplicationService(
            self.store,
            dag_store,
            _FakeWritableService(self.parent_session_id),
            self.leases,
            self.relays,
            parent_binding=self.binding,
        )
        with self.assertRaisesRegex(ConfigurationError, "publication failed"):
            await service.create_task_dag(CreateTaskDagRequest("publish-error", (_node("a", 0),)))
        with self.assertRaisesRegex(ConfigurationError, "does not match the actual binding"):
            await service.get_task_dag(RunTaskDagRequest("foreign"))

    async def test_result_without_exact_evidence_becomes_indeterminate(self) -> None:
        service = self._service(_FakeWritableService(self.parent_session_id))
        await service.create_task_dag(CreateTaskDagRequest("no-evidence", (_node("a", 0),)))
        result = await service.run_task_dag(RunTaskDagRequest("no-evidence"))
        self.assertIs(result.state, TaskDagState.INDETERMINATE)
        self.assertIs(result.node("a").state, TaskDagNodeState.INDETERMINATE)

    async def test_non_completed_worker_result_is_recorded_as_failure(self) -> None:
        writable = _FakeWritableService(self.parent_session_id)

        async def run_failed(request, *, execution_identity, sink=None):
            del request, sink
            self.leases.by_parent_task[execution_identity.parent_task_id] = self._evidence(
                execution_identity.parent_task_id,
                execution_identity.node_id,
            )
            return SimpleNamespace(status=SessionTaskStatus.FAILED, response="failed result")

        writable.run_subagent_with_execution_identity = run_failed
        service = self._service(writable)
        await service.create_task_dag(CreateTaskDagRequest("failed-result", (_node("a", 0),)))
        result = await service.run_task_dag(RunTaskDagRequest("failed-result"))
        self.assertIs(result.state, TaskDagState.FAILED)
        self.assertEqual(result.node("a").state, TaskDagNodeState.FAILED)
        self.assertEqual(
            result.node("a").error_reason, "writable worker returned a non-completed result"
        )

    async def test_claim_race_reloads_and_non_concurrent_claim_error_is_wrapped(self) -> None:
        writable = _FakeWritableService(self.parent_session_id)
        original_run = writable.run_subagent_with_execution_identity

        async def run_and_publish(request, *, execution_identity, sink=None):
            self.leases.by_parent_task[execution_identity.parent_task_id] = self._evidence(
                execution_identity.parent_task_id,
                execution_identity.node_id,
            )
            return await original_run(
                request,
                execution_identity=execution_identity,
                sink=sink,
            )

        writable.run_subagent_with_execution_identity = run_and_publish
        race_store = _IntermittentClaimStore(
            self.store,
            TaskDagError("claim race", kind="concurrent_modification"),
        )
        race_service = TaskDagApplicationService(
            self.store,
            race_store,
            writable,
            self.leases,
            self.relays,
            parent_binding=self.binding,
        )
        await race_service.create_task_dag(CreateTaskDagRequest("claim-race", (_node("a", 0),)))
        result = await race_service.run_task_dag(RunTaskDagRequest("claim-race"))
        self.assertIs(result.state, TaskDagState.COMPLETED)

        error_store = _IntermittentClaimStore(
            self.store,
            TaskDagError("claim failed", kind="command_failed"),
        )
        error_service = TaskDagApplicationService(
            self.store,
            error_store,
            _FakeWritableService(self.parent_session_id),
            self.leases,
            self.relays,
            parent_binding=self.binding,
        )
        await error_service.create_task_dag(CreateTaskDagRequest("claim-error", (_node("a", 0),)))
        with self.assertRaisesRegex(ConfigurationError, "node claim failed"):
            await error_service.run_task_dag(RunTaskDagRequest("claim-error"))

    async def test_dependency_uncertainty_and_persistence_errors_are_fail_closed(self) -> None:
        base = TaskDag.create(
            dag_id="dependency-boundaries",
            parent_session_id=self.parent_session_id,
            nodes=(_node("a", 0), _node("b", 1, ("a",))),
            created_at=_now(),
        )
        for dependency_state, reason in (
            (TaskDagNodeState.INDETERMINATE, "dependency_indeterminate"),
            (TaskDagNodeState.CANCELLED, "dependency_cancelled"),
        ):
            snapshot = replace(
                base,
                nodes=(replace(base.node("a"), state=dependency_state), base.node("b")),
            )
            dag_store = SimpleNamespace(
                compare_and_transition_task_dag_node=AsyncMock(
                    side_effect=lambda dag_id, node, snapshot=snapshot, **kwargs: replace(
                        snapshot,
                        nodes=(snapshot.node("a"), node),
                    )
                ),
                get_task_dag=AsyncMock(return_value=snapshot),
            )
            service = TaskDagApplicationService(
                self.store,
                dag_store,
                _FakeWritableService(self.parent_session_id),
                self.leases,
                self.relays,
                parent_binding=self.binding,
            )
            result = await service._propagate_dependencies(snapshot)
            self.assertEqual(result.node("b").state, TaskDagNodeState.SKIPPED)
            self.assertEqual(result.node("b").error_reason, reason)

        complete_snapshot = replace(
            base,
            nodes=(replace(base.node("a"), state=TaskDagNodeState.COMPLETED), base.node("b")),
        )
        for error, message in (
            (TaskDagError("dependency race", kind="concurrent_modification"), ""),
            (
                TaskDagError("dependency write failed", kind="command_failed"),
                "dependency propagation failed",
            ),
        ):
            dag_store = SimpleNamespace(
                compare_and_transition_task_dag_node=AsyncMock(side_effect=error),
                get_task_dag=AsyncMock(return_value=complete_snapshot),
            )
            service = TaskDagApplicationService(
                self.store,
                dag_store,
                _FakeWritableService(self.parent_session_id),
                self.leases,
                self.relays,
                parent_binding=self.binding,
            )
            if message:
                with self.assertRaisesRegex(ConfigurationError, message):
                    await service._propagate_dependencies(complete_snapshot)
            else:
                reloaded = await service._propagate_dependencies(complete_snapshot)
                self.assertEqual(reloaded, complete_snapshot)

    async def test_terminal_classification_and_transition_errors_are_bounded(self) -> None:
        base = TaskDag.create(
            dag_id="classification",
            parent_session_id=self.parent_session_id,
            nodes=(_node("a", 0), _node("b", 1, ("a",))),
            created_at=_now(),
        )
        dag_store = SimpleNamespace(
            compare_and_transition_task_dag=AsyncMock(
                side_effect=lambda proposed, **kwargs: proposed
            ),
            get_task_dag=AsyncMock(return_value=base),
        )
        service = TaskDagApplicationService(
            self.store,
            dag_store,
            _FakeWritableService(self.parent_session_id),
            self.leases,
            self.relays,
            parent_binding=self.binding,
        )
        snapshots = (
            (
                replace(
                    base,
                    nodes=(
                        replace(base.node("a"), state=TaskDagNodeState.INDETERMINATE),
                        base.node("b"),
                    ),
                ),
                TaskDagState.INDETERMINATE,
            ),
            (base, TaskDagState.INDETERMINATE),
            (
                replace(
                    base,
                    nodes=(
                        replace(base.node("a"), state=TaskDagNodeState.COMPLETED),
                        replace(base.node("b"), state=TaskDagNodeState.COMPLETED),
                    ),
                ),
                TaskDagState.COMPLETED,
            ),
            (
                replace(
                    base,
                    nodes=(
                        replace(base.node("a"), state=TaskDagNodeState.CANCELLED),
                        replace(base.node("b"), state=TaskDagNodeState.SKIPPED),
                    ),
                ),
                TaskDagState.CANCELLED,
            ),
            (
                replace(
                    base,
                    nodes=(
                        replace(base.node("a"), state=TaskDagNodeState.FAILED),
                        replace(base.node("b"), state=TaskDagNodeState.SKIPPED),
                    ),
                ),
                TaskDagState.FAILED,
            ),
        )
        for snapshot, expected in snapshots:
            classified = await service._classify_terminal_or_uncertain(snapshot)
            self.assertIs(classified.state, expected)
        self.assertIs(await service._set_graph_state_if_needed(base, TaskDagState.READY), base)

        for error, message in (
            (TaskDagError("state race", kind="concurrent_modification"), ""),
            (TaskDagError("state write failed", kind="command_failed"), "state transition failed"),
        ):
            failing_store = SimpleNamespace(
                compare_and_transition_task_dag=AsyncMock(side_effect=error),
                get_task_dag=AsyncMock(return_value=base),
            )
            failing_service = TaskDagApplicationService(
                self.store,
                failing_store,
                _FakeWritableService(self.parent_session_id),
                self.leases,
                self.relays,
                parent_binding=self.binding,
            )
            if message:
                with self.assertRaisesRegex(ConfigurationError, message):
                    await failing_service._set_graph_state_if_needed(base, TaskDagState.COMPLETED)
            else:
                self.assertEqual(
                    await failing_service._set_graph_state_if_needed(base, TaskDagState.COMPLETED),
                    base,
                )

    async def test_worker_finish_and_cancellation_errors_are_wrapped(self) -> None:
        base = TaskDag.create(
            dag_id="finish-boundaries",
            parent_session_id=self.parent_session_id,
            nodes=(_node("a", 0), _node("b", 1)),
            created_at=_now(),
        )
        running_node = replace(
            base.node("a"),
            state=TaskDagNodeState.RUNNING,
            generation=1,
            parent_task_id="worker-a",
        )
        claimed = TaskDag(
            dag_id=base.dag_id,
            parent_session_id=base.parent_session_id,
            nodes=(running_node, base.node("b")),
            state=TaskDagState.RUNNING,
            active_node_id="a",
        )
        for error, message in (
            (TaskDagError("finish race", kind="concurrent_modification"), ""),
            (TaskDagError("finish failed", kind="command_failed"), "node finish failed"),
        ):
            dag_store = SimpleNamespace(
                finish_task_dag_node=AsyncMock(side_effect=error),
                get_task_dag=AsyncMock(return_value=claimed),
            )
            service = TaskDagApplicationService(
                self.store,
                dag_store,
                _FakeWritableService(self.parent_session_id),
                self.leases,
                self.relays,
                parent_binding=self.binding,
            )
            if message:
                with self.assertRaisesRegex(ConfigurationError, message):
                    await service._finish_worker_node(
                        claimed,
                        running_node,
                        TaskDagNodeState.FAILED,
                        error=RuntimeError("worker failed"),
                    )
            else:
                self.assertEqual(
                    await service._finish_worker_node(
                        claimed,
                        running_node,
                        TaskDagNodeState.FAILED,
                        error=RuntimeError("worker failed"),
                    ),
                    claimed,
                )

        for error, message in (
            (TaskDagError("cancel race", kind="concurrent_modification"), ""),
            (TaskDagError("cancel failed", kind="command_failed"), "cancellation failed"),
        ):
            cancel_base = TaskDag.create(
                dag_id="cancel-boundary",
                parent_session_id=self.parent_session_id,
                nodes=(_node("a", 0),),
                created_at=_now(),
            )
            dag_store = SimpleNamespace(
                compare_and_transition_task_dag_node=AsyncMock(side_effect=error),
                compare_and_transition_task_dag=AsyncMock(
                    side_effect=lambda proposed, **kwargs: proposed
                ),
                get_task_dag=AsyncMock(return_value=cancel_base),
            )
            service = TaskDagApplicationService(
                self.store,
                dag_store,
                _FakeWritableService(self.parent_session_id),
                self.leases,
                self.relays,
                parent_binding=self.binding,
            )
            if message:
                with self.assertRaisesRegex(ConfigurationError, message):
                    await service._cancel_remaining_graph(cancel_base.dag_id)
            else:
                cancelled = await service._cancel_remaining_graph(cancel_base.dag_id)
                self.assertIs(cancelled.state, TaskDagState.CANCELLED)

    async def test_reconciliation_covers_missing_identity_and_nonterminal_workers(self) -> None:
        base = TaskDag.create(
            dag_id="missing-worker-identity",
            parent_session_id=self.parent_session_id,
            nodes=(_node("a", 0),),
            created_at=_now(),
        )
        running_node = replace(
            base.node("a"),
            state=TaskDagNodeState.RUNNING,
            generation=1,
            parent_task_id="worker-a",
        )
        claimed = TaskDag(
            dag_id=base.dag_id,
            parent_session_id=base.parent_session_id,
            nodes=(running_node,),
            state=TaskDagState.RUNNING,
            active_node_id="a",
        )
        object.__setattr__(running_node, "parent_task_id", None)
        dag_store = SimpleNamespace(
            finish_task_dag_node=AsyncMock(
                side_effect=lambda dag_id, node, **kwargs: replace(
                    claimed,
                    nodes=(node,),
                    active_node_id=None,
                )
            ),
        )
        service = TaskDagApplicationService(
            self.store,
            dag_store,
            _FakeWritableService(self.parent_session_id),
            self.leases,
            self.relays,
            parent_binding=self.binding,
        )
        reconciled = await service._reconcile_active_node(claimed)
        self.assertIs(reconciled.node("a").state, TaskDagNodeState.INDETERMINATE)

    async def test_diamond_failure_skips_only_descendants_and_keeps_independent_branch(
        self,
    ) -> None:
        writable = _FakeWritableService(
            self.parent_session_id,
            outcomes={
                "prompt-a": "A complete",
                "prompt-b": RuntimeError("B failed"),
                "prompt-c": "C complete",
            },
        )
        original_run = writable.run_subagent_with_execution_identity

        async def run_and_publish(request, *, execution_identity, sink=None):
            self.leases.by_parent_task[execution_identity.parent_task_id] = self._evidence(
                execution_identity.parent_task_id,
                execution_identity.node_id,
            )
            return await original_run(
                request,
                execution_identity=execution_identity,
                sink=sink,
            )

        writable.run_subagent_with_execution_identity = run_and_publish
        service = self._service(writable)
        await service.create_task_dag(
            CreateTaskDagRequest(
                "diamond",
                (
                    _node("a", 0),
                    _node("b", 1, ("a",)),
                    _node("c", 2),
                    _node("d", 3, ("b", "c")),
                ),
            )
        )

        result = await service.run_task_dag(RunTaskDagRequest("diamond"))
        self.assertIs(result.state, TaskDagState.FAILED)
        self.assertEqual(
            {node.node_id: node.state for node in result.nodes},
            {
                "a": TaskDagNodeState.COMPLETED,
                "b": TaskDagNodeState.FAILED,
                "c": TaskDagNodeState.COMPLETED,
                "d": TaskDagNodeState.SKIPPED,
            },
        )
        self.assertEqual([call[0] for call in writable.calls], ["prompt-a", "prompt-b", "prompt-c"])
        self.assertEqual(writable.max_active, 1)
        self.assertEqual(result.node("d").error_reason, "dependency_failed")
        self.assertEqual(
            len({node.worktree_id for node in result.nodes if node.worktree_id is not None}),
            3,
        )
        self.assertTrue(all(node.parent_task_id for node in result.nodes[:3]))

    def _parallel_service(
        self,
        state: _ParallelRunState,
        *,
        failures: set[str] = (),
        dependency_relays: _FakeDependencyRelayStore | None = None,
    ) -> tuple[TaskDagApplicationService, _ParallelWritableFactory]:
        def record_evidence(parent_task_id: str, node_id: str) -> None:
            root = Path(self._temporary.name)
            parent_root = root / "parallel-parent"
            repository = WorktreeRepositoryIdentity(
                common_dir=root,
                source_worktree=parent_root,
                git_dir=root / "git",
                head_sha="a" * 40,
            )
            worktree_id = WorktreeId(f"parallel-{node_id}")
            now = _now()
            self.leases.by_parent_task[parent_task_id] = WritableSubagentWorkspaceLease(
                lease_id=f"lease-{node_id}",
                parent_session_id=self.parent_session_id,
                parent_task_id=parent_task_id,
                worktree_id=worktree_id,
                parent_capability_fingerprint="a" * 64,
                parent_workspace_root=parent_root,
                parent_repository=repository,
                base_commit_sha="a" * 40,
                canonical_child_root=root / "parallel-managed" / node_id,
                state=WritableSubagentWorkspaceState.PRESERVED,
                created_at=now,
                updated_at=now,
                baseline_checkpoint_id=CheckpointId(f"cp-{node_id}"),
                child_session_id=f"child-{node_id}",
                final_workspace_fingerprint="b" * 64,
                changed_file_count=0,
            )

        factory = _ParallelWritableFactory(
            self.parent_session_id,
            state,
            record_evidence,
            failures,
        )
        base = _ParallelWritableService(
            self.parent_session_id,
            state,
            record_evidence,
            failures,
        )
        service = TaskDagApplicationService(
            self.store,
            self.store,
            base,
            self.leases,
            self.relays,
            parent_binding=self.binding,
            writable_worker_factory=factory,
            dependency_relay_store=dependency_relays,
        )
        return service, factory

    async def test_parallel_fanout_has_real_overlap_and_fanin_waits_for_both(self) -> None:
        state = _ParallelRunState({"b", "c"})
        relay_store = _FakeDependencyRelayStore()
        service, factory = self._parallel_service(state, dependency_relays=relay_store)
        await service.create_task_dag(
            CreateTaskDagRequest(
                "parallel-fanout",
                (
                    _node("a", 0),
                    _node("b", 1, ("a",)),
                    _node("c", 2, ("a",)),
                    _node("d", 3, ("b", "c")),
                ),
                max_parallel=2,
            )
        )
        running = asyncio.create_task(service.run_task_dag(RunTaskDagRequest("parallel-fanout")))
        await asyncio.wait_for(state.started_event.wait(), timeout=10)
        snapshot = await self.store.get_task_dag("parallel-fanout")
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot.running_node_ids, ("b", "c"))
        self.assertIsNone(snapshot.active_node_id)
        self.assertEqual(state.max_active, 2)
        self.assertEqual(len(factory.services), 3)
        self.assertEqual(len({service.service_id for service in factory.services}), 3)
        self.assertEqual(state.invocation_count.get("b"), 1)
        self.assertEqual(state.invocation_count.get("c"), 1)
        self.assertNotIn("d", state.started_nodes)
        state.release.set()
        result = await asyncio.wait_for(running, timeout=20)
        self.assertIs(result.state, TaskDagState.COMPLETED)
        self.assertEqual(
            [node.state for node in result.nodes],
            [
                TaskDagNodeState.COMPLETED,
                TaskDagNodeState.COMPLETED,
                TaskDagNodeState.COMPLETED,
                TaskDagNodeState.COMPLETED,
            ],
        )
        self.assertGreater(state.started_nodes.index("d"), state.started_nodes.index("b"))
        self.assertGreater(state.started_nodes.index("d"), state.started_nodes.index("c"))
        self.assertLessEqual(max(state.invocation_count.values()), 1)
        fanin_relay = state.dependency_relays["d"]
        self.assertEqual(fanin_relay.direct_dependency_ids, ("b", "c"))
        self.assertEqual(
            tuple(entry.predecessor_node_id for entry in fanin_relay.entries),
            ("b", "c"),
        )
        self.assertEqual(
            await relay_store.get_task_dag_dependency_relay(fanin_relay.relay_id),
            fanin_relay,
        )

    async def test_leader_wave_claims_only_selected_nodes_and_enforces_capacity(self) -> None:
        state = _ParallelRunState({"b", "c"})
        service, factory = self._parallel_service(state)
        await service.create_task_dag(
            CreateTaskDagRequest(
                "leader-wave",
                (_node("b", 0), _node("c", 1), _node("e", 2)),
                max_parallel=2,
            )
        )
        running = asyncio.create_task(
            service.run_task_dag_wave(
                RunTaskDagWaveRequest(
                    "leader-wave",
                    ("b", "c"),
                    expected_dag_generation=0,
                    expected_node_generations=(("b", 0), ("c", 0)),
                )
            )
        )
        await asyncio.wait_for(state.started_event.wait(), timeout=10)
        snapshot = await self.store.get_task_dag("leader-wave")
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot.running_node_ids, ("b", "c"))
        self.assertNotIn("e", state.started_nodes)
        self.assertEqual(len(factory.services), 2)
        state.release.set()
        result = await asyncio.wait_for(running, timeout=20)
        self.assertEqual(
            [node.state for node in result.nodes],
            [TaskDagNodeState.COMPLETED, TaskDagNodeState.COMPLETED, TaskDagNodeState.READY],
        )

        await service.create_task_dag(
            CreateTaskDagRequest(
                "leader-wave-overflow",
                (_node("a", 0), _node("b", 1), _node("c", 2)),
                max_parallel=2,
            )
        )
        with self.assertRaisesRegex(ConfigurationError, "exceeds"):
            await service.run_task_dag_wave(
                RunTaskDagWaveRequest(
                    "leader-wave-overflow",
                    ("a", "b", "c"),
                    expected_dag_generation=0,
                    expected_node_generations=(("a", 0), ("b", 0), ("c", 0)),
                )
            )

    async def test_leader_wave_partial_claim_does_not_substitute_unselected_node(self) -> None:
        state = _ParallelRunState({"b"})
        service, _ = self._parallel_service(state)
        await service.create_task_dag(
            CreateTaskDagRequest(
                "leader-wave-partial",
                (_node("b", 0), _node("c", 1), _node("e", 2)),
                max_parallel=2,
            )
        )
        delegate = self.store

        class RaceStore:
            def __init__(self) -> None:
                self.changed = False

            def __getattr__(self, name: str) -> object:
                return getattr(delegate, name)

            async def claim_task_dag_node(self, *args, **kwargs):
                result = await delegate.claim_task_dag_node(*args, **kwargs)
                if not self.changed:
                    self.changed = True
                    current = await delegate.get_task_dag("leader-wave-partial")
                    assert current is not None
                    c = current.node("c")
                    await delegate.compare_and_transition_task_dag_node(
                        "leader-wave-partial",
                        replace(
                            c,
                            state=TaskDagNodeState.CANCELLED,
                            generation=c.generation + 1,
                            error_kind="race",
                            error_reason="selected node changed",
                        ),
                        expected_generation=c.generation,
                        expected_state=TaskDagNodeState.READY,
                    )
                return result

        service._dag_store = RaceStore()  # type: ignore[assignment]
        running = asyncio.create_task(
            service.run_task_dag_wave(
                RunTaskDagWaveRequest(
                    "leader-wave-partial",
                    ("b", "c"),
                    expected_dag_generation=0,
                    expected_node_generations=(("b", 0), ("c", 0)),
                )
            )
        )
        await asyncio.wait_for(state.started_event.wait(), timeout=10)
        self.assertEqual(state.started_nodes, ["b"])
        self.assertNotIn("e", state.started_nodes)
        state.release.set()
        result = await asyncio.wait_for(running, timeout=20)
        self.assertEqual(result.node("b").state, TaskDagNodeState.COMPLETED)
        self.assertEqual(result.node("c").state, TaskDagNodeState.CANCELLED)
        self.assertEqual(result.node("e").state, TaskDagNodeState.READY)

    async def test_parallel_capacity_does_not_start_third_ready_node_until_slot_frees(self) -> None:
        state = _ParallelRunState({"b", "c"})
        service, _ = self._parallel_service(state)
        await service.create_task_dag(
            CreateTaskDagRequest(
                "parallel-capacity",
                (_node("b", 0), _node("c", 1), _node("e", 2)),
                max_parallel=2,
            )
        )
        running = asyncio.create_task(service.run_task_dag(RunTaskDagRequest("parallel-capacity")))
        await asyncio.wait_for(state.started_event.wait(), timeout=10)
        self.assertEqual(set(state.started_nodes), {"b", "c"})
        snapshot = await self.store.get_task_dag("parallel-capacity")
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(len(snapshot.running_node_ids), 2)
        self.assertNotIn("e", state.started_nodes)
        state.release.set()
        result = await asyncio.wait_for(running, timeout=20)
        self.assertIs(result.state, TaskDagState.COMPLETED)
        self.assertEqual(state.invocation_count, {"b": 1, "c": 1, "e": 1})
        self.assertLessEqual(state.max_active, 2)

    async def test_parallel_branch_failure_keeps_sibling_running(self) -> None:
        state = _ParallelRunState({"c"})
        service, _ = self._parallel_service(state, failures={"b"})
        await service.create_task_dag(
            CreateTaskDagRequest(
                "parallel-failure",
                (_node("b", 0), _node("c", 1)),
                max_parallel=2,
            )
        )
        running = asyncio.create_task(service.run_task_dag(RunTaskDagRequest("parallel-failure")))
        await asyncio.wait_for(state.started_event.wait(), timeout=10)
        snapshot = await self.store.get_task_dag("parallel-failure")
        for _ in range(100):
            if snapshot is not None and snapshot.node("b").state is TaskDagNodeState.FAILED:
                break
            await asyncio.sleep(0.01)
            snapshot = await self.store.get_task_dag("parallel-failure")
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot.running_node_ids, ("c",))
        self.assertEqual(snapshot.node("b").state, TaskDagNodeState.FAILED)
        self.assertNotIn("b", snapshot.running_node_ids)
        state.release.set()
        result = await asyncio.wait_for(running, timeout=20)
        self.assertIs(result.state, TaskDagState.FAILED)
        self.assertIs(result.node("c").state, TaskDagNodeState.COMPLETED)

    async def test_indeterminate_branch_does_not_block_unrelated_ready_branch(self) -> None:
        state = _ParallelRunState()
        service, _ = self._parallel_service(state)
        await service.create_task_dag(
            CreateTaskDagRequest(
                "parallel-indeterminate-sibling",
                (_node("b", 0), _node("c", 1)),
                max_parallel=2,
            )
        )
        snapshot = await self.store.get_task_dag("parallel-indeterminate-sibling")
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        await self.store.claim_task_dag_node(
            snapshot.dag_id,
            replace(
                snapshot.node("b"),
                state=TaskDagNodeState.RUNNING,
                generation=1,
                parent_task_id="dead-b",
                execution_owner_pid=999_999_999,
                execution_owner_token="dead-b-owner",
            ),
            expected_generation=0,
            expected_state=TaskDagNodeState.READY,
            updated_at=_now(),
        )
        result = await service.run_task_dag(RunTaskDagRequest(snapshot.dag_id))
        self.assertIs(result.state, TaskDagState.INDETERMINATE)
        self.assertIs(result.node("b").state, TaskDagNodeState.INDETERMINATE)
        self.assertIs(result.node("c").state, TaskDagNodeState.COMPLETED)
        self.assertEqual(state.invocation_count, {"c": 1})

    async def test_parallel_cancellation_finishes_all_owned_nodes_structurally(self) -> None:
        state = _ParallelRunState({"b", "c"})
        service, _ = self._parallel_service(state)
        await service.create_task_dag(
            CreateTaskDagRequest(
                "parallel-cancel",
                (_node("b", 0), _node("c", 1), _node("e", 2)),
                max_parallel=2,
            )
        )
        running = asyncio.create_task(service.run_task_dag(RunTaskDagRequest("parallel-cancel")))
        await asyncio.wait_for(state.started_event.wait(), timeout=10)
        running.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(running, timeout=20)
        result = await service.get_task_dag(RunTaskDagRequest("parallel-cancel"))
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(
            [node.state for node in result.nodes],
            [TaskDagNodeState.CANCELLED] * 3,
        )

    async def test_two_scheduler_instances_do_not_duplicate_the_active_node(self) -> None:
        writable_a = _FakeWritableService(self.parent_session_id, block=True)
        writable_b = _FakeWritableService(self.parent_session_id)
        service_a = self._service(writable_a)
        service_b = self._service(writable_b)
        await service_a.create_task_dag(CreateTaskDagRequest("race", (_node("a", 0),)))

        first = asyncio.create_task(service_a.run_task_dag(RunTaskDagRequest("race")))
        await writable_a.started.wait()
        second = asyncio.create_task(service_b.run_task_dag(RunTaskDagRequest("race")))
        await asyncio.sleep(0)
        self.assertEqual(writable_b.calls, [])
        _, node_id, parent_task_id = writable_a.calls[0]
        identity = writable_a.execution_identities[0]
        self.assertEqual(identity.dag_id, "race")
        self.assertEqual(identity.node_id, node_id)
        self.assertEqual(identity.parent_task_id, parent_task_id)
        self.leases.by_parent_task[parent_task_id] = self._evidence(parent_task_id, node_id)
        writable_a.release.set()
        await asyncio.gather(first, second)
        final = await service_a.run_task_dag(RunTaskDagRequest("race"))
        self.assertIs(final.state, TaskDagState.COMPLETED)
        self.assertEqual(len(writable_a.calls) + len(writable_b.calls), 1)

    async def test_cancellation_finishes_active_node_and_cancels_remaining_nodes(self) -> None:
        writable = _FakeWritableService(self.parent_session_id, block=True)
        original_run = writable.run_subagent_with_execution_identity

        async def run_and_publish(request, *, execution_identity, sink=None):
            self.leases.by_parent_task[execution_identity.parent_task_id] = self._evidence(
                execution_identity.parent_task_id,
                execution_identity.node_id,
            )
            return await original_run(
                request,
                execution_identity=execution_identity,
                sink=sink,
            )

        writable.run_subagent_with_execution_identity = run_and_publish
        service = self._service(writable)
        await service.create_task_dag(
            CreateTaskDagRequest("cancel", (_node("a", 0), _node("b", 1)))
        )
        running = asyncio.create_task(service.run_task_dag(RunTaskDagRequest("cancel")))
        await writable.started.wait()
        running.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await running
        result = await service.run_task_dag(RunTaskDagRequest("cancel"))
        self.assertIs(result.state, TaskDagState.CANCELLED)
        self.assertEqual(
            [node.state for node in result.nodes],
            [TaskDagNodeState.CANCELLED, TaskDagNodeState.CANCELLED],
        )
        self.assertEqual(len(writable.calls), 1)

    async def test_completed_result_preview_is_utf8_bounded(self) -> None:
        writable = _FakeWritableService(
            self.parent_session_id,
            outcomes={"prompt-a": "中" * 10_000},
        )
        original_run = writable.run_subagent_with_execution_identity

        async def run_and_publish(request, *, execution_identity, sink=None):
            self.leases.by_parent_task[execution_identity.parent_task_id] = self._evidence(
                execution_identity.parent_task_id,
                execution_identity.node_id,
            )
            return await original_run(
                request,
                execution_identity=execution_identity,
                sink=sink,
            )

        writable.run_subagent_with_execution_identity = run_and_publish
        service = self._service(writable)
        await service.create_task_dag(CreateTaskDagRequest("bounded", (_node("a", 0),)))
        result = await service.run_task_dag(RunTaskDagRequest("bounded"))
        self.assertIs(result.state, TaskDagState.COMPLETED)
        preview = result.node("a").response_preview
        self.assertIsNotNone(preview)
        assert preview is not None
        self.assertLessEqual(
            len(preview.encode("utf-8")),
            MAX_TASK_DAG_RESPONSE_PREVIEW_BYTES,
        )

    async def test_parent_identity_is_derived_from_binding_and_not_caller_data(self) -> None:
        wrong_parent = _FakeWritableService("another-parent")
        with self.assertRaisesRegex(ConfigurationError, "does not match binding"):
            self._service(wrong_parent)

        writable = _FakeWritableService(self.parent_session_id)
        service = self._service(writable)
        created = await service.create_task_dag(
            CreateTaskDagRequest("binding-parent", (_node("a", 0),))
        )
        self.assertEqual(created.parent_session_id, self.parent_session_id)

    async def test_reconciliation_maps_exact_worker_terminal_state_and_uncertainty(self) -> None:
        writable = _FakeWritableService(self.parent_session_id)
        service = self._service(writable)
        for index, status in enumerate(
            (
                SessionTaskStatus.COMPLETED,
                SessionTaskStatus.FAILED,
                SessionTaskStatus.CANCELLED,
            )
        ):
            dag_id = f"reconcile-{status.value}"
            task_id = f"worker-{status.value}"
            await service.create_task_dag(CreateTaskDagRequest(dag_id, (_node("a", 0),)))
            task = SessionTask(
                task_id,
                SessionTaskKind.SUBAGENT,
                SessionTaskStatus.RUNNING,
                _now() + timedelta(seconds=index),
            )
            await self.store.create_session_task(self.parent_session_id, task)
            snapshot = await self.store.get_task_dag(dag_id)
            assert snapshot is not None
            claimed = replace(
                snapshot.node("a"),
                state=TaskDagNodeState.RUNNING,
                generation=1,
                parent_task_id=task_id,
            )
            await self.store.claim_task_dag_node(
                dag_id,
                claimed,
                expected_generation=0,
                expected_state=TaskDagNodeState.READY,
                updated_at=_now(),
            )
            finished = task.finish(status, finished_at=_now() + timedelta(seconds=5))
            await self.store.update_session_task(self.parent_session_id, finished)
            self.leases.by_parent_task[task_id] = self._evidence(task_id, "a")
            reconciled = await service.reconcile_task_dag(RunTaskDagRequest(dag_id))
            expected = {
                SessionTaskStatus.COMPLETED: TaskDagNodeState.COMPLETED,
                SessionTaskStatus.FAILED: TaskDagNodeState.FAILED,
                SessionTaskStatus.CANCELLED: TaskDagNodeState.CANCELLED,
            }[status]
            self.assertIs(reconciled.node("a").state, expected)

        await service.create_task_dag(CreateTaskDagRequest("not-preserved", (_node("a", 0),)))
        completed_task = SessionTask(
            "worker-not-preserved",
            SessionTaskKind.SUBAGENT,
            SessionTaskStatus.RUNNING,
            _now(),
        )
        await self.store.create_session_task(self.parent_session_id, completed_task)
        not_preserved = await self.store.get_task_dag("not-preserved")
        assert not_preserved is not None
        await self.store.claim_task_dag_node(
            "not-preserved",
            replace(
                not_preserved.node("a"),
                state=TaskDagNodeState.RUNNING,
                generation=1,
                parent_task_id=completed_task.task_id,
            ),
            expected_generation=0,
            expected_state=TaskDagNodeState.READY,
            updated_at=_now(),
        )
        completed_task = completed_task.finish(
            SessionTaskStatus.COMPLETED,
            finished_at=_now() + timedelta(seconds=1),
        )
        await self.store.update_session_task(self.parent_session_id, completed_task)
        not_preserved_evidence = self._evidence(completed_task.task_id, "a")
        not_preserved_values = vars(cast(SimpleNamespace, not_preserved_evidence)).copy()
        not_preserved_values["state"] = WritableSubagentWorkspaceState.FAILED
        self.leases.by_parent_task[completed_task.task_id] = SimpleNamespace(
            **not_preserved_values,
        )
        not_preserved_result = await service.reconcile_task_dag(RunTaskDagRequest("not-preserved"))
        self.assertIs(
            not_preserved_result.node("a").state,
            TaskDagNodeState.INDETERMINATE,
        )

        await service.create_task_dag(CreateTaskDagRequest("still-running", (_node("a", 0),)))
        running_task = SessionTask(
            "worker-still-running",
            SessionTaskKind.SUBAGENT,
            SessionTaskStatus.RUNNING,
            _now(),
        )
        await self.store.create_session_task(self.parent_session_id, running_task)
        still_running = await self.store.get_task_dag("still-running")
        assert still_running is not None
        await self.store.claim_task_dag_node(
            "still-running",
            replace(
                still_running.node("a"),
                state=TaskDagNodeState.RUNNING,
                generation=1,
                parent_task_id=running_task.task_id,
            ),
            expected_generation=0,
            expected_state=TaskDagNodeState.READY,
            updated_at=_now(),
        )
        self.leases.by_parent_task[running_task.task_id] = self._evidence(
            running_task.task_id,
            "a",
        )
        still_running_result = await service.reconcile_task_dag(RunTaskDagRequest("still-running"))
        self.assertIs(still_running_result.node("a").state, TaskDagNodeState.RUNNING)
        self.assertEqual(still_running_result.active_node_id, "a")

        await service.create_task_dag(CreateTaskDagRequest("orphaned", (_node("a", 0),)))
        orphan_task = SessionTask(
            "worker-orphaned",
            SessionTaskKind.SUBAGENT,
            SessionTaskStatus.RUNNING,
            _now(),
        )
        await self.store.create_session_task(self.parent_session_id, orphan_task)
        orphaned = await self.store.get_task_dag("orphaned")
        assert orphaned is not None
        await self.store.claim_task_dag_node(
            "orphaned",
            replace(
                orphaned.node("a"),
                state=TaskDagNodeState.RUNNING,
                generation=1,
                parent_task_id=orphan_task.task_id,
            ),
            expected_generation=0,
            expected_state=TaskDagNodeState.READY,
            updated_at=_now(),
        )
        orphan_evidence = self._evidence(orphan_task.task_id, "a")
        orphan_values = vars(cast(SimpleNamespace, orphan_evidence)).copy()
        orphan_values["state"] = WritableSubagentWorkspaceState.ORPHANED
        self.leases.by_parent_task[orphan_task.task_id] = SimpleNamespace(
            **orphan_values,
        )
        uncertain = await service.reconcile_task_dag(RunTaskDagRequest("orphaned"))
        self.assertIs(uncertain.node("a").state, TaskDagNodeState.INDETERMINATE)
        self.assertEqual(writable.calls, [])

        await service.create_task_dag(CreateTaskDagRequest("missing", (_node("a", 0),)))
        missing = await self.store.get_task_dag("missing")
        assert missing is not None
        await self.store.claim_task_dag_node(
            "missing",
            replace(
                missing.node("a"),
                state=TaskDagNodeState.RUNNING,
                generation=1,
                parent_task_id="worker-missing",
            ),
            expected_generation=0,
            expected_state=TaskDagNodeState.READY,
            updated_at=_now(),
        )
        missing_result = await service.reconcile_task_dag(RunTaskDagRequest("missing"))
        self.assertIs(missing_result.node("a").state, TaskDagNodeState.INDETERMINATE)
        self.assertEqual(writable.calls, [])


if __name__ == "__main__":
    unittest.main()
