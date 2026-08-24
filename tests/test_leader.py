from __future__ import annotations

import asyncio
import hashlib
import json
import multiprocessing as mp
import os
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

from neuro_code.application.ports.leader import LeaderStore, LeaderStoreError
from neuro_code.application.ports.model import ModelProvider
from neuro_code.application.sessions.binding import ConversationBinding, ConversationRunner
from neuro_code.application.workflows.leader import (
    LeaderApplicationService,
    RunLeaderRequest,
    _bounded_redacted,
)
from neuro_code.application.workflows.subagent_capabilities import SubagentCapabilitySet
from neuro_code.application.workflows.task_dag import (
    CreateTaskDagRequest,
    RunTaskDagStepRequest,
    TaskDagApplicationService,
)
from neuro_code.domain.execution import TurnSource
from neuro_code.domain.leader import (
    LeaderAttempt,
    LeaderAttemptState,
    LeaderDecision,
    LeaderDecisionKind,
    LeaderDecisionRecord,
    LeaderEvidenceEnvelope,
    LeaderEvidenceNode,
)
from neuro_code.domain.sandbox.models import SandboxProfile
from neuro_code.domain.session_tasks import SessionTaskStatus
from neuro_code.domain.task_dag import (
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
    return datetime.now(UTC)


_PROCESS_TEST_TIME = datetime(2026, 1, 1, tzinfo=UTC)


def _node(node_id: str, ordinal: int, dependencies: tuple[str, ...] = ()) -> TaskDagNode:
    return TaskDagNode(
        node_id=node_id,
        ordinal=ordinal,
        prompt=f"prompt-{node_id}",
        dependencies=dependencies,
    )


class _Runner:
    def __init__(self, session_id: str, responses: list[str]) -> None:
        self._session_id = session_id
        self.responses = responses
        self.prompts: list[str] = []
        self.turn_ids: list[str | None] = []
        self.calls = 0
        self.selection_calls = 0
        self.delay: asyncio.Event | None = None
        self.started: asyncio.Event | None = None
        self.release: asyncio.Event | None = None

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def items(self) -> tuple[object, ...]:
        return ()

    async def run(
        self,
        prompt: str,
        *,
        sink=None,
        content_parts=(),
        cancellation_policy=None,
        turn_source: TurnSource = TurnSource.USER,
        turn_id: str | None = None,
    ) -> object:
        del sink, content_parts, cancellation_policy
        if turn_source is not TurnSource.USER:
            raise AssertionError("Leader must use the user turn source")
        if self.delay is not None:
            await self.delay.wait()
        self.prompts.append(prompt)
        self.turn_ids.append(turn_id)
        self.calls += 1
        if self.started is not None:
            self.started.set()
        if self.release is not None:
            await self.release.wait()
        response = self.responses.pop(0)
        if '"action":"SELECT_NODE"' in response:
            self.selection_calls += 1
        return SimpleNamespace(response=response)


def _binding(runner: _Runner, *, zero_tools: bool = False) -> ConversationBinding:
    capabilities = None
    if zero_tools:
        capabilities = SubagentCapabilitySet.from_runtime(
            tool_names=(),
            cwd=Path.cwd(),
            sandbox_profile=SandboxProfile.OFF,
            enable_background_tasks=False,
            max_steps=1,
        )
    return ConversationBinding(
        cast(ConversationRunner, runner),
        cast(ModelProvider, object()),
        capabilities=capabilities,
    )


class _LeaseStore:
    def __init__(self, parent_session_id: str) -> None:
        self.parent_session_id = parent_session_id
        self.by_parent_task: dict[str, object] = {}

    async def get_writable_subagent_lease_for_parent_task(
        self,
        parent_session_id: str,
        parent_task_id: str,
    ) -> object | None:
        if parent_session_id != self.parent_session_id:
            return None
        return self.by_parent_task.get(parent_task_id)


class _RelayStore:
    async def get_parent_context_relay_for_lease(self, lease_id: str) -> object:
        return SimpleNamespace(relay_id=f"relay-{lease_id}")


class _Writable:
    def __init__(self, parent_session_id: str, leases: _LeaseStore) -> None:
        self.parent_session_id = parent_session_id
        self.leases = leases
        self.calls: list[str] = []

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
        self.calls.append(node_id)
        parent_task_id = execution_identity.parent_task_id
        self.leases.by_parent_task[parent_task_id] = SimpleNamespace(
            parent_session_id=self.parent_session_id,
            parent_task_id=parent_task_id,
            child_session_id=f"child-{node_id}",
            lease_id=f"lease-{node_id}-{len(self.calls)}",
            worktree_id=WorktreeId(f"worktree-{node_id}-{len(self.calls)}"),
            baseline_checkpoint_id=None,
            final_workspace_fingerprint=None,
            changed_file_count=0,
            state=WritableSubagentWorkspaceState.PRESERVED,
        )
        return SimpleNamespace(status=SessionTaskStatus.COMPLETED, response=request.prompt)


class _ProcessDagController:
    """Small durable test controller for process-death Leader boundaries."""

    def __init__(
        self,
        dag_id: str,
        parent_session_id: str,
        state_path: str,
        worker_marker_path: str,
        *,
        crash_after_worker: bool = False,
    ) -> None:
        self.dag_id = dag_id
        self.parent_session_id = parent_session_id
        self.state_path = Path(state_path)
        self.worker_marker_path = Path(worker_marker_path)
        self.crash_after_worker = crash_after_worker

    def _snapshot(self) -> TaskDag:
        base = TaskDag.create(
            dag_id=self.dag_id,
            parent_session_id=self.parent_session_id,
            nodes=(_node("a", 0),),
            created_at=_PROCESS_TEST_TIME,
        )
        if self.state_path.read_text(encoding="utf-8") != "completed":
            return base
        completed = replace(
            base.node("a"),
            state=TaskDagNodeState.COMPLETED,
            generation=1,
            response_preview="worker complete",
            final_workspace_fingerprint="a" * 64,
            changed_file_count=0,
        )
        return replace(
            base,
            nodes=(completed,),
            state=TaskDagState.COMPLETED,
            generation=1,
            updated_at=_PROCESS_TEST_TIME + timedelta(seconds=1),
        )

    async def prepare_task_dag_step(self, request) -> TaskDag:
        if request.dag_id != self.dag_id:
            raise ConfigurationError("unknown process test DAG")
        return self._snapshot()

    async def run_task_dag_step(self, request, *, sink=None) -> TaskDag:
        del sink
        if request.dag_id != self.dag_id or request.selected_node_id != "a":
            raise ConfigurationError("selected process test node is invalid")
        if self.state_path.read_text(encoding="utf-8") == "completed":
            raise ConfigurationError("process test worker was already executed")
        with self.worker_marker_path.open("a", encoding="utf-8") as marker:
            marker.write("worker\n")
        self.state_path.write_text("completed", encoding="utf-8")
        if self.crash_after_worker:
            os._exit(73)
        return self._snapshot()


class _MarkerRunner(_Runner):
    def __init__(self, session_id: str, responses: list[str], marker_path: str) -> None:
        super().__init__(session_id, responses)
        self.marker_path = Path(marker_path)

    async def run(self, prompt: str, **kwargs) -> object:
        result = await super().run(prompt, **kwargs)
        action = str(json.loads(result.response)["action"])
        with self.marker_path.open("a", encoding="utf-8") as marker:
            marker.write(f"{action}\n")
        return result


class _ExitBeforeProviderRunner(_Runner):
    def __init__(self, session_id: str) -> None:
        super().__init__(session_id, [])

    async def run(self, prompt: str, **kwargs) -> object:
        del prompt, kwargs
        os._exit(71)


class _ExitAfterModelCommitStore:
    def __init__(self, inner: SqliteSessionStore) -> None:
        self.inner = inner

    def __getattr__(self, name: str):
        return getattr(self.inner, name)

    async def mark_leader_model_committed(self, *args, **kwargs):
        await self.inner.mark_leader_model_committed(*args, **kwargs)
        os._exit(72)


class _ExitAfterDecisionStore:
    def __init__(self, inner: SqliteSessionStore) -> None:
        self.inner = inner

    def __getattr__(self, name: str):
        return getattr(self.inner, name)

    async def publish_leader_decision(self, *args, **kwargs):
        await self.inner.publish_leader_decision(*args, **kwargs)
        os._exit(72)


def _leader_process_child(
    mode: str,
    database_path: str,
    dag_id: str,
    parent_session_id: str,
    leader_session_id: str,
    state_path: str,
    worker_marker_path: str,
    provider_marker_path: str,
) -> None:
    asyncio.run(
        _leader_process_child_async(
            mode,
            database_path,
            dag_id,
            parent_session_id,
            leader_session_id,
            state_path,
            worker_marker_path,
            provider_marker_path,
        )
    )


async def _leader_process_child_async(
    mode: str,
    database_path: str,
    dag_id: str,
    parent_session_id: str,
    leader_session_id: str,
    state_path: str,
    worker_marker_path: str,
    provider_marker_path: str,
) -> None:
    store = SqliteSessionStore(Path(database_path))
    await store.initialize()
    parent_binding = _binding(_Runner(parent_session_id, []))
    if mode == "before_model":
        leader_runner = _ExitBeforeProviderRunner(leader_session_id)
    else:
        leader_runner = _MarkerRunner(
            leader_session_id,
            ['{"action":"SELECT_NODE","node_id":"a"}'],
            provider_marker_path,
        )
    leader_binding = _binding(leader_runner, zero_tools=True)
    controller = _ProcessDagController(
        dag_id,
        parent_session_id,
        state_path,
        worker_marker_path,
        crash_after_worker=mode == "after_worker",
    )
    leader_store = store
    if mode == "after_model_commit":
        leader_store = _ExitAfterModelCommitStore(store)
    elif mode == "after_decision":
        leader_store = _ExitAfterDecisionStore(store)

    def clock() -> datetime:
        return _PROCESS_TEST_TIME

    service = LeaderApplicationService(
        leader_store,
        controller,
        parent_binding=parent_binding,
        leader_binding=leader_binding,
        session_store=store,
        clock=clock,
        lease_seconds=1.0,
    )
    await service.run(RunLeaderRequest(dag_id, "process crash objective"))


class LeaderDomainTests(unittest.TestCase):
    def test_evidence_is_deterministic_and_bounded(self) -> None:
        prompt_fingerprint = hashlib.sha256(b"prompt-a").hexdigest()
        node = LeaderEvidenceNode(
            node_id="a",
            ordinal=0,
            dependencies=(),
            state=TaskDagNodeState.READY,
            prompt="prompt-a",
            prompt_fingerprint=prompt_fingerprint,
            response_preview="token=secret",
        )
        first = LeaderEvidenceEnvelope(
            objective="objective",
            dag_id="dag",
            definition_fingerprint="a" * 64,
            generation=0,
            state=TaskDagState.READY,
            active_node_id=None,
            ready_node_ids=("a",),
            nodes=(node,),
        )
        second = LeaderEvidenceEnvelope(
            objective="objective",
            dag_id="dag",
            definition_fingerprint="a" * 64,
            generation=0,
            state=TaskDagState.READY,
            active_node_id=None,
            ready_node_ids=("a",),
            nodes=(node,),
        )
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(first.fingerprint, first.to_dict()["evidence_fingerprint"])
        with self.assertRaisesRegex(ValueError, "unknown"):
            LeaderDecision.parse('{"action":"CREATE_NODE","node_id":"a"}')
        with self.assertRaisesRegex(ValueError, "strict JSON"):
            LeaderDecision.parse('```json\n{"action":"FINALIZE"}\n```')

    def test_typed_decision_contract_is_fail_closed(self) -> None:
        selected = LeaderDecision.parse(
            '{"action":"SELECT_NODE","node_id":"b","reason":"run bash /etc/passwd"}'
        )
        self.assertIs(selected.kind, LeaderDecisionKind.SELECT_NODE)
        self.assertEqual(selected.selected_node_id, "b")
        finalized = LeaderDecision.parse('{"action":"FINALIZE","summary":"done"}')
        self.assertIs(finalized.kind, LeaderDecisionKind.FINALIZE)
        with self.assertRaises(ValueError):
            LeaderDecision.parse('{"action":"SELECT_NODE","node_id":"missing","extra":1}')

    def test_domain_bounds_and_lifecycle_values_fail_closed(self) -> None:
        prompt_fingerprint = hashlib.sha256(b"prompt-a").hexdigest()
        node = LeaderEvidenceNode(
            node_id="a",
            ordinal=0,
            dependencies=(),
            state=TaskDagNodeState.READY,
            prompt="prompt-a",
            prompt_fingerprint=prompt_fingerprint,
        )
        evidence_kwargs = {
            "objective": "objective",
            "dag_id": "dag",
            "definition_fingerprint": "a" * 64,
            "generation": 0,
            "state": TaskDagState.READY,
            "active_node_id": None,
            "ready_node_ids": ("a",),
            "nodes": (node,),
        }
        node_kwargs = {
            "node_id": "a",
            "ordinal": 0,
            "dependencies": (),
            "state": TaskDagNodeState.READY,
            "prompt": "prompt-a",
            "prompt_fingerprint": prompt_fingerprint,
        }
        invalid_nodes = (
            {"node_id": ""},
            {"ordinal": True},
            {"dependencies": ["a"]},
            {"state": "ready"},
            {"prompt": ""},
            {"prompt_fingerprint": "bad"},
            {"changed_file_count": -1},
            {"final_workspace_fingerprint": "bad"},
            {"parent_task_id": "bad\x00id"},
        )
        for changes in invalid_nodes:
            with self.assertRaises(ValueError):
                LeaderEvidenceNode(**{**node_kwargs, **changes})
        for invalid in (
            {"prompt": "x" * 2_049},
            {"response_preview": "x" * 2_049},
            {"error_reason": "x" * 1_025},
        ):
            with self.assertRaises(ValueError):
                LeaderEvidenceNode(**{**node_kwargs, **invalid})
        for changes in (
            {"objective": ""},
            {"generation": True},
            {"state": "ready"},
            {"active_node_id": "bad\x00id"},
            {"ready_node_ids": ["a"]},
            {"nodes": (replace(node, ordinal=1),)},
            {
                "nodes": tuple(
                    replace(node, node_id=f"node-{index}", ordinal=index) for index in range(9)
                )
            },
        ):
            with self.assertRaises(ValueError):
                LeaderEvidenceEnvelope(**{**evidence_kwargs, **changes})
        for response in (
            "",
            "[]",
            "{}",
            '{"action":1}',
            '{"action":"SELECT_NODE"}',
            '{"action":"SELECT_NODE","node_id":"a","reason":1}',
            '{"action":"FINALIZE","summary":1}',
            '{"action":"FINALIZE","node_id":"a"}',
        ):
            with self.assertRaises(ValueError):
                LeaderDecision.parse(response)
        with self.assertRaises(ValueError):
            LeaderDecision(LeaderDecisionKind.SELECT_NODE)
        with self.assertRaises(ValueError):
            LeaderDecision(LeaderDecisionKind.FINALIZE, selected_node_id="a")
        with self.assertRaises(ValueError):
            LeaderAttempt(
                "attempt",
                "dag",
                "session",
                "a" * 64,
                True,
                "b" * 64,
                "c" * 64,
                LeaderAttemptState.CLAIMED,
                "owner",
                _now(),
                "turn",
            )
        with self.assertRaises(ValueError):
            LeaderAttempt(
                "attempt",
                "dag",
                "session",
                "a" * 64,
                0,
                "b" * 64,
                "c" * 64,
                LeaderAttemptState.CLAIMED,
                "owner",
                datetime.fromtimestamp(0, UTC).replace(tzinfo=None),
                "turn",
            )
        with self.assertRaises(ValueError):
            LeaderDecisionRecord(
                "decision",
                "attempt",
                "dag",
                "session",
                True,
                "b" * 64,
                "c" * 64,
                LeaderDecision(LeaderDecisionKind.FINALIZE),
                _now(),
            )
        with self.assertRaises(ValueError):
            LeaderDecisionRecord(
                "decision",
                "attempt",
                "dag",
                "session",
                0,
                "b" * 64,
                "c" * 64,
                LeaderDecision(LeaderDecisionKind.FINALIZE),
                datetime.fromtimestamp(0, UTC).replace(tzinfo=None),
            )
        self.assertTrue(LeaderAttemptState.CLAIMED.can_transition_to(LeaderAttemptState.STALE))
        self.assertFalse(LeaderAttemptState.EXECUTED.can_transition_to(LeaderAttemptState.STALE))


class LeaderIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temporary.name) / "sessions.db"
        self.store = SqliteSessionStore(self.database_path)
        await self.store.initialize()
        self.parent_session_id = await self.store.create_session(
            self._temporary.name,
            "fixture",
            "fixture-model",
        )
        self.leader_session_id = await self.store.create_session(
            self._temporary.name,
            "fixture",
            "fixture-model",
        )
        self.parent_runner = _Runner(self.parent_session_id, [])
        self.leader_runner = _Runner(
            self.leader_session_id,
            [
                '{"action":"SELECT_NODE","node_id":"b"}',
                '{"action":"SELECT_NODE","node_id":"a"}',
                '{"action":"SELECT_NODE","node_id":"c"}',
                '{"action":"SELECT_NODE","node_id":"d"}',
                '{"action":"FINALIZE","summary":"all bounded steps complete"}',
            ],
        )
        self.parent_binding = _binding(self.parent_runner)
        self.leader_binding = _binding(self.leader_runner, zero_tools=True)
        self.leases = _LeaseStore(self.parent_session_id)
        self.writable = _Writable(self.parent_session_id, self.leases)
        self.dag_service = TaskDagApplicationService(
            self.store,
            self.store,
            self.writable,
            self.leases,
            _RelayStore(),
            parent_binding=self.parent_binding,
        )

    async def asyncTearDown(self) -> None:
        self._temporary.cleanup()

    async def _create_dag(self) -> None:
        await self.dag_service.create_task_dag(
            CreateTaskDagRequest(
                "diamond",
                (
                    _node("a", 0),
                    _node("b", 1),
                    _node("c", 2, ("a",)),
                    _node("d", 3, ("b", "c")),
                ),
            )
        )

    def _leader(self, runner: _Runner | None = None) -> LeaderApplicationService:
        binding = self.leader_binding if runner is None else _binding(runner, zero_tools=True)
        return LeaderApplicationService(
            cast(LeaderStore, self.store),
            self.dag_service,
            parent_binding=self.parent_binding,
            leader_binding=binding,
            session_store=self.store,
            redaction_values=("secret-value",),
        )

    async def test_leader_constructor_requires_zero_tool_capabilities(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "zero tools"):
            LeaderApplicationService(
                cast(LeaderStore, self.store),
                self.dag_service,
                parent_binding=self.parent_binding,
                leader_binding=_binding(_Runner(self.leader_session_id, [])),
                session_store=self.store,
            )
        with self.assertRaisesRegex(ConfigurationError, "lease"):
            LeaderApplicationService(
                cast(LeaderStore, self.store),
                self.dag_service,
                parent_binding=self.parent_binding,
                leader_binding=self.leader_binding,
                lease_seconds=0.5,
            )
        with self.assertRaises(ValueError):
            await self._leader().run(cast(RunLeaderRequest, object()))

    async def test_request_and_binding_contracts_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "DAG id"):
            RunLeaderRequest("", "objective")
        with self.assertRaisesRegex(ValueError, "must not be empty"):
            RunLeaderRequest("dag", " ")
        with self.assertRaisesRegex(ValueError, "control character"):
            RunLeaderRequest("dag", "bad\x00objective")
        with self.assertRaisesRegex(ValueError, "too large"):
            RunLeaderRequest("dag", "x" * 4_097)

        service = self._leader()
        self.assertEqual(service.leader_session_id, self.leader_session_id)
        self.assertTrue(service.owner_id)
        await service.close()

        with self.assertRaisesRegex(ConfigurationError, "parent binding"):
            LeaderApplicationService(
                cast(LeaderStore, self.store),
                self.dag_service,
                parent_binding=cast(ConversationBinding, object()),
                leader_binding=self.leader_binding,
            )
        with self.assertRaisesRegex(ConfigurationError, "model binding"):
            LeaderApplicationService(
                cast(LeaderStore, self.store),
                self.dag_service,
                parent_binding=self.parent_binding,
                leader_binding=cast(ConversationBinding, object()),
            )
        with self.assertRaisesRegex(ConfigurationError, "DAG service"):
            LeaderApplicationService(
                cast(LeaderStore, self.store),
                cast(object, object()),
                parent_binding=self.parent_binding,
                leader_binding=self.leader_binding,
            )
        with self.assertRaisesRegex(ConfigurationError, "parent session"):
            LeaderApplicationService(
                cast(LeaderStore, self.store),
                self.dag_service,
                parent_binding=_binding(_Runner("", [])),
                leader_binding=self.leader_binding,
            )
        with self.assertRaisesRegex(ConfigurationError, "model session"):
            LeaderApplicationService(
                cast(LeaderStore, self.store),
                self.dag_service,
                parent_binding=self.parent_binding,
                leader_binding=_binding(_Runner("", []), zero_tools=True),
            )
        with self.assertRaisesRegex(ConfigurationError, "owner"):
            LeaderApplicationService(
                cast(LeaderStore, self.store),
                self.dag_service,
                parent_binding=self.parent_binding,
                leader_binding=self.leader_binding,
                owner_id="",
            )

    async def test_bounded_redaction_and_active_dag_return_are_fail_closed(self) -> None:
        self.assertIsNone(
            _bounded_redacted(
                None,
                limit=8,
                field_name="test field",
                explicit_values=(),
            )
        )
        with self.assertRaisesRegex(ConfigurationError, "not text"):
            _bounded_redacted(
                cast(str | None, object()),
                limit=8,
                field_name="test field",
                explicit_values=(),
            )
        self.assertEqual(
            _bounded_redacted(
                "abcdef",
                limit=3,
                field_name="test field",
                explicit_values=(),
            ),
            "abc",
        )
        with (
            patch(
                "neuro_code.application.workflows.leader.redact_sensitive_text",
                return_value="",
            ),
            self.assertRaisesRegex(ConfigurationError, "became empty"),
        ):
            _bounded_redacted(
                "secret",
                limit=8,
                field_name="test field",
                explicit_values=(),
            )

        dag = TaskDag.create(
            dag_id="active",
            parent_session_id=self.parent_session_id,
            nodes=(_node("a", 0),),
            created_at=_now(),
        )
        running = replace(
            dag,
            nodes=(
                replace(
                    dag.node("a"),
                    state=TaskDagNodeState.RUNNING,
                    parent_task_id="parent-active",
                ),
            ),
            state=TaskDagState.RUNNING,
            generation=1,
            updated_at=_now(),
            active_node_id="a",
        )

        class ActiveController:
            async def prepare_task_dag_step(self, request):
                del request
                return running

            async def run_task_dag_step(self, request, *, sink=None):
                del request, sink
                raise AssertionError("an active DAG must not start another worker")

        active_service = LeaderApplicationService(
            cast(LeaderStore, self.store),
            ActiveController(),
            parent_binding=self.parent_binding,
            leader_binding=self.leader_binding,
            session_store=self.store,
        )
        result = await active_service.run(RunLeaderRequest("active", "objective"))
        self.assertIsNone(result.final_response)
        self.assertFalse(result.terminal)

    async def test_provider_and_durable_failures_do_not_replay(self) -> None:
        await self.dag_service.create_task_dag(
            CreateTaskDagRequest("provider-error", (_node("a", 0),))
        )
        failing_runner = _Runner(self.leader_session_id, [])

        async def fail_provider(*args, **kwargs):
            del args, kwargs
            raise RuntimeError("provider failed")

        failing_runner.run = fail_provider  # type: ignore[method-assign]
        with self.assertRaisesRegex(ConfigurationError, "model turn failed"):
            await self._leader(failing_runner).run(RunLeaderRequest("provider-error", "objective"))

        original_lookup = self.store.get_leader_attempt_for_snapshot

        async def fail_lookup(*args, **kwargs):
            del args, kwargs
            raise LeaderStoreError("lookup failed")

        self.store.get_leader_attempt_for_snapshot = fail_lookup  # type: ignore[method-assign]
        try:
            await self.dag_service.create_task_dag(
                CreateTaskDagRequest("lookup-error", (_node("a", 0),))
            )
            with self.assertRaisesRegex(ConfigurationError, "durable lookup"):
                await self._leader().run(RunLeaderRequest("lookup-error", "objective"))
        finally:
            self.store.get_leader_attempt_for_snapshot = original_lookup  # type: ignore[method-assign]

        original_claim = self.store.claim_leader_attempt

        async def fail_claim(*args, **kwargs):
            del args, kwargs
            raise LeaderStoreError("claim failed")

        self.store.claim_leader_attempt = fail_claim  # type: ignore[method-assign]
        try:
            await self.dag_service.create_task_dag(
                CreateTaskDagRequest("claim-error", (_node("a", 0),))
            )
            with self.assertRaisesRegex(ConfigurationError, "durable claim"):
                await self._leader().run(RunLeaderRequest("claim-error", "objective"))
        finally:
            self.store.claim_leader_attempt = original_claim  # type: ignore[method-assign]

        original_publish = self.store.publish_leader_decision

        async def fail_publish(*args, **kwargs):
            del args, kwargs
            raise LeaderStoreError("publish failed")

        self.store.publish_leader_decision = fail_publish  # type: ignore[method-assign]
        try:
            await self.dag_service.create_task_dag(
                CreateTaskDagRequest("publish-error", (_node("a", 0),))
            )
            runner = _Runner(
                self.leader_session_id,
                ['{"action":"SELECT_NODE","node_id":"a"}'],
            )
            with self.assertRaisesRegex(ConfigurationError, "typed decision durability"):
                await self._leader(runner).run(RunLeaderRequest("publish-error", "objective"))
            self.assertEqual(runner.calls, 1)
        finally:
            self.store.publish_leader_decision = original_publish  # type: ignore[method-assign]

    async def test_recovery_helpers_fail_closed_at_each_durable_boundary(self) -> None:
        await self.dag_service.create_task_dag(
            CreateTaskDagRequest("helper-boundaries", (_node("a", 0),))
        )
        dag = await self.store.get_task_dag("helper-boundaries")
        assert dag is not None
        service = self._leader()
        evidence = service._evidence("objective", dag)
        now = _now()

        def attempt(
            suffix: str,
            state: LeaderAttemptState,
            *,
            model_response: str | None = None,
            decision_id: str | None = None,
            lease_expires_at: datetime | None = None,
        ) -> LeaderAttempt:
            return LeaderAttempt(
                attempt_id=f"leader-attempt-helper-{suffix}",
                dag_id=dag.dag_id,
                leader_session_id=self.leader_session_id,
                objective_fingerprint=hashlib.sha256(b"objective").hexdigest(),
                dag_generation=dag.generation,
                definition_fingerprint=dag.definition_fingerprint,
                evidence_fingerprint=evidence.fingerprint,
                state=state,
                owner_id="leader-owner-helper",
                lease_expires_at=lease_expires_at or now + timedelta(seconds=30),
                turn_id=f"leader-turn-helper-{suffix}",
                model_response=model_response,
                decision_id=decision_id,
            )

        with self.assertRaisesRegex(ConfigurationError, "no response"):
            await service._reuse_durable_decision(
                attempt("missing-response", LeaderAttemptState.MODEL_COMMITTED),
                evidence,
            )
        with self.assertRaisesRegex(ConfigurationError, "cannot be reused"):
            await service._reuse_durable_decision(
                attempt(
                    "bad-response",
                    LeaderAttemptState.MODEL_COMMITTED,
                    model_response="not-json",
                ),
                evidence,
            )
        with self.assertRaisesRegex(ConfigurationError, "owns this"):
            await service._reuse_durable_decision(
                attempt("claimed", LeaderAttemptState.CLAIMED),
                evidence,
            )
        with self.assertRaisesRegex(ConfigurationError, "recovery is required"):
            await service._reuse_durable_decision(
                attempt("stale", LeaderAttemptState.STALE),
                evidence,
            )

        with self.assertRaisesRegex(ConfigurationError, "no decision identity"):
            await service._load_decision(
                attempt("no-decision-id", LeaderAttemptState.DECISION_PUBLISHED),
                evidence,
            )
        with self.assertRaisesRegex(ConfigurationError, "decision is missing"):
            await service._load_decision(
                attempt(
                    "missing-decision",
                    LeaderAttemptState.DECISION_PUBLISHED,
                    decision_id="leader-decision-missing",
                ),
                evidence,
            )
        original_get_decision = self.store.get_leader_decision

        async def fail_get_decision(*args, **kwargs):
            del args, kwargs
            raise LeaderStoreError("decision lookup failed")

        self.store.get_leader_decision = fail_get_decision  # type: ignore[method-assign]
        try:
            with self.assertRaisesRegex(ConfigurationError, "decision lookup"):
                await service._load_decision(
                    attempt(
                        "lookup-error",
                        LeaderAttemptState.DECISION_PUBLISHED,
                        decision_id="leader-decision-error",
                    ),
                    evidence,
                )
        finally:
            self.store.get_leader_decision = original_get_decision  # type: ignore[method-assign]

        expired = attempt(
            "expired",
            LeaderAttemptState.CLAIMED,
            lease_expires_at=now - timedelta(seconds=1),
        )
        no_recovery_service = LeaderApplicationService(
            cast(LeaderStore, self.store),
            self.dag_service,
            parent_binding=self.parent_binding,
            leader_binding=self.leader_binding,
        )
        with self.assertRaisesRegex(ConfigurationError, "inspection is unavailable"):
            await no_recovery_service._guard_existing_claim(expired, now)

        original_load_turn_attempts = self.store.load_turn_attempts

        async def fail_load_turn_attempts(*args, **kwargs):
            del args, kwargs
            raise RuntimeError("recovery inspection failed")

        self.store.load_turn_attempts = fail_load_turn_attempts  # type: ignore[method-assign]
        try:
            with self.assertRaisesRegex(ConfigurationError, "inspection failed"):
                await service._guard_existing_claim(expired, now)
        finally:
            self.store.load_turn_attempts = original_load_turn_attempts  # type: ignore[method-assign]

        async def unresolved_turn(*args, **kwargs):
            del args, kwargs
            return (SimpleNamespace(turn_id=expired.turn_id),)

        self.store.load_turn_attempts = unresolved_turn  # type: ignore[method-assign]
        try:
            with self.assertRaisesRegex(ConfigurationError, "unresolved provider turn"):
                await service._guard_existing_claim(expired, now)
        finally:
            self.store.load_turn_attempts = original_load_turn_attempts  # type: ignore[method-assign]

        await service._mark_indeterminate(
            attempt("already-indeterminate", LeaderAttemptState.INDETERMINATE)
        )
        await service._mark_stale(attempt("already-stale", LeaderAttemptState.STALE))
        await service._mark_stale(attempt("already-executed", LeaderAttemptState.EXECUTED))
        await service._mark_executed(attempt("already-executed-again", LeaderAttemptState.EXECUTED))

        original_transition = self.store.transition_leader_attempt

        async def fail_transition(*args, **kwargs):
            del args, kwargs
            raise LeaderStoreError("transition raced", kind="concurrent_modification")

        self.store.transition_leader_attempt = fail_transition  # type: ignore[method-assign]
        try:
            await service._mark_indeterminate(
                attempt("indeterminate-race", LeaderAttemptState.CLAIMED)
            )
            await service._mark_stale(attempt("stale-race", LeaderAttemptState.CLAIMED))
            await service._mark_executed(
                attempt("executed-race", LeaderAttemptState.DECISION_PUBLISHED)
            )
        finally:
            self.store.transition_leader_attempt = original_transition  # type: ignore[method-assign]

        async def fail_transition_hard(*args, **kwargs):
            del args, kwargs
            raise LeaderStoreError("transition failed", kind="integrity")

        self.store.transition_leader_attempt = fail_transition_hard  # type: ignore[method-assign]
        try:
            with self.assertRaisesRegex(ConfigurationError, "completion failed"):
                await service._mark_executed(
                    attempt("executed-failure", LeaderAttemptState.DECISION_PUBLISHED)
                )
        finally:
            self.store.transition_leader_attempt = original_transition  # type: ignore[method-assign]

        mismatched = LeaderDecisionRecord(
            decision_id="leader-decision-mismatch",
            attempt_id="leader-attempt-other",
            dag_id=dag.dag_id,
            leader_session_id=self.leader_session_id,
            dag_generation=dag.generation,
            definition_fingerprint=dag.definition_fingerprint,
            evidence_fingerprint=evidence.fingerprint,
            decision=LeaderDecision(LeaderDecisionKind.SELECT_NODE, selected_node_id="a"),
            created_at=now,
        )
        with self.assertRaisesRegex(ConfigurationError, "identity"):
            service._validate_decision(
                mismatched,
                attempt("identity", LeaderAttemptState.DECISION_PUBLISHED),
                evidence,
            )

    async def test_invalid_typed_decision_is_durable_stale_and_not_replayed(self) -> None:
        await self.dag_service.create_task_dag(CreateTaskDagRequest("invalid", (_node("a", 0),)))
        runner = _Runner(self.leader_session_id, ['{"action":"FINALIZE"}'])
        with self.assertRaisesRegex(ConfigurationError, "terminal"):
            await self._leader(runner).run(RunLeaderRequest("invalid", "objective"))
        dag = await self.store.get_task_dag("invalid")
        assert dag is not None
        evidence = self._leader(runner)._evidence("objective", dag)
        attempt = await self.store.get_leader_attempt_for_snapshot(
            "invalid",
            dag_generation=dag.generation,
            definition_fingerprint=dag.definition_fingerprint,
            evidence_fingerprint=evidence.fingerprint,
            objective_fingerprint=hashlib.sha256(b"objective").hexdigest(),
        )
        assert attempt is not None
        self.assertIs(attempt.state, LeaderAttemptState.STALE)
        self.assertEqual(runner.calls, 1)

    async def test_empty_or_malformed_provider_output_becomes_indeterminate(self) -> None:
        for dag_id, response in (("empty", ""), ("malformed", "not-json")):
            await self.dag_service.create_task_dag(CreateTaskDagRequest(dag_id, (_node("a", 0),)))
            runner = _Runner(self.leader_session_id, [response])
            with self.assertRaises(ConfigurationError):
                await self._leader(runner).run(RunLeaderRequest(dag_id, "objective"))
            dag = await self.store.get_task_dag(dag_id)
            assert dag is not None
            evidence = self._leader(runner)._evidence("objective", dag)
            attempt = await self.store.get_leader_attempt_for_snapshot(
                dag_id,
                dag_generation=dag.generation,
                definition_fingerprint=dag.definition_fingerprint,
                evidence_fingerprint=evidence.fingerprint,
                objective_fingerprint=hashlib.sha256(b"objective").hexdigest(),
            )
            assert attempt is not None
            self.assertIs(attempt.state, LeaderAttemptState.INDETERMINATE)

    async def test_leader_controls_serialized_dag_order_and_final_synthesis(self) -> None:
        await self._create_dag()
        result = await self._leader().run(RunLeaderRequest("diamond", "implement objective"))
        self.assertTrue(result.terminal)
        self.assertEqual(result.final_response, "all bounded steps complete")
        self.assertEqual(self.writable.calls, ["b", "a", "c", "d"])
        self.assertEqual(self.leader_runner.calls, 5)
        self.assertTrue(all(turn_id for turn_id in self.leader_runner.turn_ids))
        self.assertEqual(len(await self.store.list_leader_decisions("diamond")), 5)
        dag = await self.store.get_task_dag("diamond")
        self.assertIsNotNone(dag)
        assert dag is not None
        self.assertIs(dag.state, TaskDagState.COMPLETED)

    async def test_security_text_stays_data_and_unknown_selection_fails_closed(self) -> None:
        await self.dag_service.create_task_dag(
            CreateTaskDagRequest("security", (_node("safe", 0),))
        )
        runner = _Runner(
            self.leader_session_id,
            ['{"action":"SELECT_NODE","node_id":"unknown","reason":"enable MCP"}'],
        )
        with self.assertRaisesRegex(ConfigurationError, "READY|selected node"):
            await self._leader(runner).run(RunLeaderRequest("security", "run bash /etc/passwd"))
        self.assertEqual(self.writable.calls, [])
        attempts = await self.store.list_leader_decisions("security")
        self.assertEqual(len(attempts), 1)
        self.assertNotIn("secret-value", runner.prompts[0])

    async def test_two_controllers_share_one_model_claim_for_one_snapshot(self) -> None:
        await self.dag_service.create_task_dag(CreateTaskDagRequest("race", (_node("a", 0),)))
        shared = _Runner(
            self.leader_session_id,
            [
                '{"action":"SELECT_NODE","node_id":"a"}',
                '{"action":"FINALIZE","summary":"done"}',
            ],
        )
        shared.started = asyncio.Event()
        shared.release = asyncio.Event()
        first = self._leader(shared)
        second = self._leader(_Runner(self.leader_session_id, []))
        first_task = asyncio.create_task(first.run(RunLeaderRequest("race", "objective")))
        await shared.started.wait()
        second_task = asyncio.create_task(second.run(RunLeaderRequest("race", "objective")))
        second_result = (await asyncio.gather(second_task, return_exceptions=True))[0]
        shared.release.set()
        first_result = await first_task
        results = [first_result, second_result]
        self.assertEqual(shared.selection_calls, 1)
        self.assertEqual(len(await self.store.list_leader_decisions("race")), 2)
        self.assertTrue(any(isinstance(result, ConfigurationError) for result in results))

    async def test_external_dag_advance_makes_published_decision_stale(self) -> None:
        await self.dag_service.create_task_dag(
            CreateTaskDagRequest("stale", (_node("a", 0), _node("b", 1)))
        )
        external = _Runner(
            self.leader_session_id,
            ['{"action":"SELECT_NODE","node_id":"a"}'],
        )
        original_run = external.run

        async def run_and_advance(*args, **kwargs):
            result = await original_run(*args, **kwargs)
            await self.dag_service.run_task_dag_step(RunTaskDagStepRequest("stale", "a"))
            return result

        external.run = run_and_advance  # type: ignore[method-assign]
        with self.assertRaisesRegex(ConfigurationError, "stale"):
            await self._leader(external).run(RunLeaderRequest("stale", "objective"))
        self.assertEqual(self.writable.calls, ["a"])

    async def test_durable_model_commit_is_reused_without_provider_replay(self) -> None:
        await self.dag_service.create_task_dag(CreateTaskDagRequest("reuse", (_node("a", 0),)))
        first_runner = _Runner(
            self.leader_session_id,
            ['{"action":"SELECT_NODE","node_id":"a"}'],
        )
        first = self._leader(first_runner)
        original_mark = self.store.mark_leader_model_committed

        async def mark_then_fail(*args, **kwargs):
            committed = await original_mark(*args, **kwargs)
            raise LeaderStoreError(
                f"crash-after-commit:{committed.attempt_id}",
                kind="simulated_crash",
            )

        self.store.mark_leader_model_committed = mark_then_fail  # type: ignore[method-assign]
        with self.assertRaisesRegex(ConfigurationError, "durability failed"):
            await first.run(RunLeaderRequest("reuse", "objective"))
        self.store.mark_leader_model_committed = original_mark  # type: ignore[method-assign]
        second_runner = _Runner(
            self.leader_session_id,
            [
                '{"action":"FINALIZE","summary":"done"}',
            ],
        )
        result = await self._leader(second_runner).run(RunLeaderRequest("reuse", "objective"))
        self.assertTrue(result.terminal)
        self.assertEqual(first_runner.calls, 1)
        self.assertEqual(second_runner.calls, 1)
        self.assertEqual(self.writable.calls, ["a"])

    async def test_sqlite_leader_lifecycle_is_idempotent_and_cas_bound(self) -> None:
        await self.dag_service.create_task_dag(CreateTaskDagRequest("lifecycle", (_node("a", 0),)))
        dag = await self.store.get_task_dag("lifecycle")
        assert dag is not None
        evidence = self._leader()._evidence("objective", dag)
        now = _now()
        objective_fingerprint = hashlib.sha256(b"objective").hexdigest()
        attempt = LeaderAttempt(
            "leader-attempt-lifecycle",
            dag.dag_id,
            self.leader_session_id,
            objective_fingerprint,
            dag.generation,
            dag.definition_fingerprint,
            evidence.fingerprint,
            LeaderAttemptState.CLAIMED,
            "leader-owner-lifecycle",
            now + timedelta(seconds=30),
            "leader-turn-lifecycle",
        )
        claim = await self.store.claim_leader_attempt(attempt, now=now)
        self.assertTrue(claim.acquired)
        rival = replace(
            attempt,
            attempt_id="leader-attempt-rival",
            owner_id="leader-owner-rival",
            turn_id="leader-turn-rival",
        )
        duplicate_claim = await self.store.claim_leader_attempt(rival, now=now)
        self.assertFalse(duplicate_claim.acquired)
        self.assertEqual(duplicate_claim.attempt.attempt_id, attempt.attempt_id)

        response = '{"action":"SELECT_NODE","node_id":"a"}'
        committed = await self.store.mark_leader_model_committed(
            attempt.attempt_id,
            owner_id=attempt.owner_id,
            turn_id=attempt.turn_id,
            model_response=response,
            updated_at=now,
        )
        self.assertIs(
            (
                await self.store.mark_leader_model_committed(
                    attempt.attempt_id,
                    owner_id=attempt.owner_id,
                    turn_id=attempt.turn_id,
                    model_response=response,
                    updated_at=now,
                )
            ).state,
            LeaderAttemptState.MODEL_COMMITTED,
        )
        with self.assertRaises(LeaderStoreError):
            await self.store.mark_leader_model_committed(
                attempt.attempt_id,
                owner_id=attempt.owner_id,
                turn_id=attempt.turn_id,
                model_response='{"action":"FINALIZE"}',
                updated_at=now,
            )
        decision = LeaderDecision(LeaderDecisionKind.SELECT_NODE, selected_node_id="a")
        published = await self.store.publish_leader_decision(
            committed.attempt_id,
            owner_id=attempt.owner_id,
            decision_id="leader-decision-lifecycle",
            decision=decision,
            created_at=now,
        )
        self.assertEqual(
            (
                await self.store.publish_leader_decision(
                    committed.attempt_id,
                    owner_id="leader-owner-recovery",
                    decision_id="leader-decision-retry",
                    decision=decision,
                    created_at=now,
                )
            ).decision_id,
            published.decision_id,
        )
        with self.assertRaises(LeaderStoreError):
            await self.store.publish_leader_decision(
                committed.attempt_id,
                owner_id=attempt.owner_id,
                decision_id="leader-decision-conflict",
                decision=LeaderDecision(LeaderDecisionKind.FINALIZE),
                created_at=now,
            )
        loaded = await self.store.get_leader_attempt_for_snapshot(
            dag.dag_id,
            dag_generation=dag.generation,
            definition_fingerprint=dag.definition_fingerprint,
            evidence_fingerprint=evidence.fingerprint,
            objective_fingerprint=objective_fingerprint,
        )
        assert loaded is not None
        self.assertEqual(loaded.decision_id, published.decision_id)
        self.assertEqual(len(await self.store.list_leader_decisions(dag.dag_id)), 1)
        executed = await self.store.transition_leader_attempt(
            loaded.attempt_id,
            expected_state=LeaderAttemptState.DECISION_PUBLISHED,
            state=LeaderAttemptState.EXECUTED,
            updated_at=now,
        )
        self.assertIs(executed.state, LeaderAttemptState.EXECUTED)
        self.assertIs(
            (
                await self.store.transition_leader_attempt(
                    loaded.attempt_id,
                    expected_state=LeaderAttemptState.DECISION_PUBLISHED,
                    state=LeaderAttemptState.EXECUTED,
                    updated_at=now,
                )
            ).state,
            LeaderAttemptState.EXECUTED,
        )
        with self.assertRaises(LeaderStoreError):
            await self.store.transition_leader_attempt(
                loaded.attempt_id,
                expected_state=LeaderAttemptState.EXECUTED,
                state=LeaderAttemptState.STALE,
                updated_at=now,
            )

    async def test_expired_claim_with_existing_turn_is_not_replayed(self) -> None:
        await self.dag_service.create_task_dag(
            CreateTaskDagRequest("indeterminate", (_node("a", 0),))
        )
        now = _now()
        attempt = LeaderAttempt(
            attempt_id="leader-attempt-old",
            dag_id="indeterminate",
            leader_session_id=self.leader_session_id,
            objective_fingerprint="a" * 64,
            dag_generation=0,
            definition_fingerprint="b" * 64,
            evidence_fingerprint="c" * 64,
            state=LeaderAttemptState.CLAIMED,
            owner_id="leader-owner-old",
            lease_expires_at=now - timedelta(seconds=1),
            turn_id="leader-turn-old",
        )
        await self.store.claim_leader_attempt(attempt, now=now - timedelta(seconds=2))
        with self.assertRaises(LeaderStoreError):
            await self.store.transition_leader_attempt(
                attempt.attempt_id,
                expected_state=LeaderAttemptState.CLAIMED,
                state=LeaderAttemptState.EXECUTED,
                updated_at=now,
            )


class LeaderProcessCrashTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        root = Path(self._temporary.name)
        self.database_path = root / "sessions.db"
        self.state_path = root / "dag-state.txt"
        self.worker_marker_path = root / "workers.log"
        self.provider_marker_path = root / "providers.log"
        self.state_path.write_text("ready", encoding="utf-8")
        self.store = SqliteSessionStore(self.database_path)
        await self.store.initialize()
        self.parent_session_id = await self.store.create_session(
            self._temporary.name,
            "fixture",
            "fixture-model",
        )
        self.leader_session_id = await self.store.create_session(
            self._temporary.name,
            "fixture",
            "fixture-model",
        )

    async def asyncTearDown(self) -> None:
        self._temporary.cleanup()

    async def _seed_dag(self, dag_id: str) -> None:
        await self.store.insert_task_dag(
            TaskDag.create(
                dag_id=dag_id,
                parent_session_id=self.parent_session_id,
                nodes=(_node("a", 0),),
                created_at=_PROCESS_TEST_TIME,
            )
        )

    async def _run_case(self, mode: str) -> None:
        dag_id = f"process-{mode}"
        await self._seed_dag(dag_id)
        context = mp.get_context("spawn")
        child = context.Process(
            target=_leader_process_child,
            args=(
                mode,
                str(self.database_path),
                dag_id,
                self.parent_session_id,
                self.leader_session_id,
                str(self.state_path),
                str(self.worker_marker_path),
                str(self.provider_marker_path),
            ),
        )
        child.start()
        await asyncio.to_thread(child.join, 30)
        self.assertFalse(child.is_alive())
        self.assertEqual(
            child.exitcode, 73 if mode == "after_worker" else 72 if mode != "before_model" else 71
        )

        parent_runner = _MarkerRunner(
            self.leader_session_id,
            (
                ['{"action":"SELECT_NODE","node_id":"a"}', '{"action":"FINALIZE","summary":"done"}']
                if mode == "before_model"
                else ['{"action":"FINALIZE","summary":"done"}']
            ),
            str(self.provider_marker_path),
        )
        parent_clock = (
            (lambda: _PROCESS_TEST_TIME + timedelta(seconds=2))
            if mode == "before_model"
            else (lambda: _PROCESS_TEST_TIME)
        )
        parent_service = LeaderApplicationService(
            self.store,
            _ProcessDagController(
                dag_id,
                self.parent_session_id,
                str(self.state_path),
                str(self.worker_marker_path),
            ),
            parent_binding=_binding(_Runner(self.parent_session_id, [])),
            leader_binding=_binding(parent_runner, zero_tools=True),
            session_store=self.store,
            clock=parent_clock,
            lease_seconds=1.0,
        )
        result = await parent_service.run(RunLeaderRequest(dag_id, "process crash objective"))
        self.assertTrue(result.terminal)
        provider_actions = self.provider_marker_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(provider_actions.count("SELECT_NODE"), 1)
        self.assertEqual(provider_actions.count("FINALIZE"), 1)
        worker_actions = self.worker_marker_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(worker_actions, ["worker"])
        self.assertEqual(self.state_path.read_text(encoding="utf-8"), "completed")

    async def test_crash_before_provider_request_recovers_after_expired_claim(self) -> None:
        await self._run_case("before_model")

    async def test_crash_after_model_commit_does_not_replay_provider(self) -> None:
        await self._run_case("after_model_commit")

    async def test_crash_after_decision_does_not_duplicate_worker(self) -> None:
        await self._run_case("after_decision")

    async def test_crash_after_worker_completion_does_not_rerun_worker(self) -> None:
        await self._run_case("after_worker")


if __name__ == "__main__":
    unittest.main()
