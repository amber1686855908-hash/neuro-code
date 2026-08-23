from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

from neuro_code.application.ports.model import ModelProvider
from neuro_code.application.ports.task_dag import TaskDagError
from neuro_code.application.sessions.binding import ConversationBinding, ConversationRunner
from neuro_code.application.workflows.task_dag import (
    CreateTaskDagRequest,
    RunTaskDagRequest,
    TaskDagApplicationService,
)
from neuro_code.domain.session_tasks import SessionTask, SessionTaskKind, SessionTaskStatus
from neuro_code.domain.task_dag import (
    MAX_TASK_DAG_NODES,
    MAX_TASK_DAG_RESPONSE_PREVIEW_BYTES,
    TaskDag,
    TaskDagNode,
    TaskDagNodeState,
    TaskDagState,
)
from neuro_code.domain.worktree import WorktreeId
from neuro_code.domain.writable_subagent import WritableSubagentWorkspaceState
from neuro_code.infrastructure.persistence.sqlite_session import SqliteSessionStore
from neuro_code.shared.errors import ConfigurationError


def _now() -> datetime:
    return datetime(2026, 8, 24, 12, tzinfo=UTC)


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


class TaskDagDomainTests(unittest.TestCase):
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
        with self.assertRaisesRegex(TaskDagError, "active|changed by another scheduler"):
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

    async def test_schema_17_migrates_to_18_and_creates_dag_tables(self) -> None:
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
        self.assertEqual(version, (18,))
        self.assertTrue({"task_dags", "task_dag_nodes"}.issubset(tables))
        self.assertIsNotNone(await reopened.get_session(self.parent_session_id))


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
