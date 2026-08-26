from __future__ import annotations

import asyncio
import json
import multiprocessing as mp
import os
import sqlite3
import subprocess
import tempfile
from collections.abc import AsyncIterator, Callable
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NoReturn, cast
from unittest.mock import patch

import pytest

from neuro_code.application.permissions.policy import PermissionMode
from neuro_code.application.ports.model import ModelCapabilitySet, ModelProvider, ModelToolPolicy
from neuro_code.application.ports.storage import SessionStore
from neuro_code.application.ports.task_dag import TaskDagStore
from neuro_code.application.ports.task_dag_replan import (
    TaskDagReplanStore,
    TaskDagReplanStoreError,
)
from neuro_code.application.sessions.binding import ConversationBinding, ConversationRunner
from neuro_code.application.settings import ApplicationSettings
from neuro_code.application.workflows.leader import RunLeaderRequest
from neuro_code.application.workflows.model_planning import RunModelDagPlanningRequest
from neuro_code.application.workflows.subagent_capabilities import SubagentCapabilitySet
from neuro_code.application.workflows.task_dag import CreateTaskDagRequest
from neuro_code.application.workflows.task_dag_replan import (
    MAX_DAG_REPLAN_LEASE_SECONDS,
    RunTaskDagReplanRequest,
    TaskDagReplanApplicationService,
)
from neuro_code.bootstrap.composition import ApplicationComposition
from neuro_code.domain.conversation.events import ModelCompleted, ModelEvent
from neuro_code.domain.execution import TurnInput, TurnRecoveryAttempt, TurnSource
from neuro_code.domain.model_planning import ModelDagProposal
from neuro_code.domain.sandbox.models import SandboxProfile
from neuro_code.domain.task_dag import TaskDag, TaskDagNode, TaskDagNodeState, TaskDagState
from neuro_code.domain.task_dag_replan import (
    MAX_DAG_REPLAN_COMPLETED_RESULT_BYTES,
    MAX_DAG_REPLAN_PROMPT_BYTES,
    DagReplanAttempt,
    DagReplanAttemptState,
    DagReplanEvidenceNode,
    DagReplanProposalRecord,
    TaskDagReplanEvidenceEnvelope,
)
from neuro_code.infrastructure.persistence.sqlite_session import SCHEMA_VERSION, SqliteSessionStore
from neuro_code.shared.errors import ConfigurationError

_REPLAN_RESPONSE = json.dumps(
    {
        "nodes": [
            {"id": "fix-a", "prompt": "repair the failed unit", "depends_on": []},
            {"id": "fix-b", "prompt": "verify the repair", "depends_on": ["fix-a"]},
        ],
        "max_parallel": 1,
        "reason": "bounded recovery",
    },
    ensure_ascii=False,
)

_REPLAN_PRODUCTION_RESPONSE = json.dumps(
    {
        "nodes": [
            {"id": "repair-b", "prompt": "planning-node-b recovery", "depends_on": []},
            {"id": "repair-c", "prompt": "planning-node-c recovery", "depends_on": []},
            {
                "id": "repair-d",
                "prompt": "planning-node-d validation",
                "depends_on": ["repair-b", "repair-c"],
            },
        ],
        "max_parallel": 2,
        "reason": "recover failed branch without rerunning completed work",
    },
    ensure_ascii=False,
)


def _write_durable_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _append_durable_json_line(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def _read_durable_json_lines(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    return [
        cast(dict[str, object], json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class _ProductionPlanningState:
    def __init__(
        self,
        *,
        provider_call_log: Path | None = None,
        provider_started_event: Any | None = None,
        provider_release_event: Any | None = None,
        fail_source_worker_ids: frozenset[str] = frozenset(),
    ) -> None:
        self.planner_calls = 0
        self.leader_calls = 0
        self.zero_tool_calls = 0
        self.provider_call_log = provider_call_log
        self.provider_started_event = provider_started_event
        self.provider_release_event = provider_release_event
        self.fail_source_worker_ids = fail_source_worker_ids
        self.replan_mode = False
        self.worker_calls: list[str] = []
        self.worker_call_phases: list[tuple[str, str]] = []
        self.started: list[str] = []
        self.timeline: list[str] = []
        self.active = 0
        self.max_active = 0
        self.lock = asyncio.Lock()
        self.fanout_started = asyncio.Event()
        self.release_fanout = asyncio.Event()


class _ProductionPlanningProvider:
    provider_name = "fixture"
    model_name = "fixture-model"
    context_affinity = "fixture-model-planning"
    capabilities = ModelCapabilitySet.all_unknown()

    def __init__(self, state: _ProductionPlanningState) -> None:
        self._state = state

    async def stream(
        self,
        context: Any,
        tools: Any,
        *,
        tool_policy: ModelToolPolicy = ModelToolPolicy.ALLOWED,
    ) -> AsyncIterator[ModelEvent]:
        del tool_policy
        contents = "\n".join(message.content for message in context.messages)
        if "bounded DAG Planner" in contents:
            if tools:
                raise AssertionError("Planner received tools")
            self._state.planner_calls += 1
            self._state.zero_tool_calls += 1
            response = json.dumps(
                {
                    "nodes": [
                        {"id": "a", "prompt": "planning-node-a", "depends_on": []},
                        {"id": "b", "prompt": "planning-node-b", "depends_on": ["a"]},
                        {"id": "c", "prompt": "planning-node-c", "depends_on": ["a"]},
                        {
                            "id": "d",
                            "prompt": "planning-node-d",
                            "depends_on": ["b", "c"],
                        },
                    ],
                    "max_parallel": 2,
                    "reason": "bounded production decomposition",
                }
            )
            if self._state.provider_call_log is not None:
                _append_durable_json_line(
                    self._state.provider_call_log,
                    {"response": response},
                )
            if self._state.provider_started_event is not None:
                self._state.provider_started_event.set()
                if self._state.provider_release_event is None:
                    raise AssertionError("provider release event is missing")
                await asyncio.to_thread(self._state.provider_release_event.wait, 90)
            yield ModelCompleted("stop", response_text=response)
            return
        if "Leader decision authority" in contents:
            if tools:
                raise AssertionError("Leader received tools")
            actions: tuple[str, ...]
            if self._state.fail_source_worker_ids and "repair-b" not in contents:
                actions = (
                    '{"action":"SELECT_NODE","node_id":"a"}',
                    '{"action":"SELECT_NODES","node_ids":["b","c"]}',
                    '{"action":"FINALIZE","summary":"source DAG failed safely"}',
                )
            else:
                actions = (
                    '{"action":"SELECT_NODE","node_id":"a"}',
                    '{"action":"SELECT_NODES","node_ids":["b","c"]}',
                    '{"action":"SELECT_NODE","node_id":"d"}',
                    '{"action":"FINALIZE","summary":"planned DAG completed"}',
                )
            if self._state.leader_calls >= len(actions):
                raise AssertionError("Leader fixture received too many decisions")
            self._state.leader_calls += 1
            self._state.zero_tool_calls += 1
            yield ModelCompleted("stop", response_text=actions[self._state.leader_calls - 1])
            return
        node_id = next(
            (
                candidate
                for candidate in ("a", "b", "c", "d")
                if f"planning-node-{candidate}" in contents
            ),
            None,
        )
        if node_id is None:
            raise AssertionError("worker fixture could not identify its node")
        if not tools:
            raise AssertionError("Writable worker unexpectedly received no tools")
        self._state.worker_calls.append(node_id)
        phase = "replan" if self._state.replan_mode else "source"
        self._state.worker_call_phases.append((node_id, phase))
        async with self._state.lock:
            self._state.started.append(node_id)
            self._state.timeline.append(f"start:{node_id}")
            self._state.active += 1
            self._state.max_active = max(self._state.max_active, self._state.active)
            if {"b", "c"}.issubset(self._state.started):
                self._state.fanout_started.set()
        try:
            if phase == "source" and node_id in self._state.fail_source_worker_ids:
                raise RuntimeError(f"fixture source worker {node_id} failed")
            if node_id in {"b", "c"}:
                await self._state.release_fanout.wait()
            yield ModelCompleted("stop", response_text=f"production result {node_id}")
        finally:
            async with self._state.lock:
                self._state.active -= 1
                self._state.timeline.append(f"complete:{node_id}")


def _run_git(directory: Path, *arguments: str) -> bytes:
    result = subprocess.run(
        ["git", *arguments],
        cwd=directory,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr.decode(errors="replace"))
    return result.stdout


def _make_repository(root: Path) -> Path:
    repository = root / "parent-repository"
    repository.mkdir()
    _run_git(repository, "init", "-q")
    _run_git(repository, "config", "user.email", "neuro-code-tests@example.invalid")
    _run_git(repository, "config", "user.name", "Neuro Code Tests")
    (repository / "tracked.txt").write_text("committed\n", encoding="utf-8")
    _run_git(repository, "add", "tracked.txt")
    _run_git(repository, "commit", "-qm", "initial")
    return repository


def _write_fixture_config(state_dir: Path) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "config.toml").write_text(
        """
[web_search]
mode = "disabled"

[web_fetch]
mode = "disabled"

[routing]
default = "fixture"

[providers.fixture]
protocol = "openai-chat"
model = "fixture-model"
base_url = "https://provider.invalid/v1"
api_key_env = "FIXTURE_KEY"
context_window_tokens = 131072
""",
        encoding="utf-8",
    )


def _production_planning_settings(repository: Path) -> ApplicationSettings:
    return ApplicationSettings(
        cwd=repository,
        sandbox="off",
        permission_mode=PermissionMode.BYPASS,
        max_steps=8,
    )


def _production_planning_environment(root: Path, state_dir: Path) -> dict[str, str]:
    return {
        "HOME": str(root),
        "NEURO_CODE_HOME": str(state_dir),
        "FIXTURE_KEY": "fixture-key",
    }


def _parent_capability(repository: Path) -> SubagentCapabilitySet:
    return SubagentCapabilitySet.from_runtime(
        tool_names=(
            "read_file",
            "read_files",
            "list_dir",
            "list_tree",
            "glob",
            "grep",
            "grep_many",
            "skill",
            "search_replace",
            "apply_patch",
        ),
        cwd=repository,
        sandbox_profile=SandboxProfile.OFF,
        enable_background_tasks=False,
        max_steps=8,
    )


class _Runner:
    def __init__(
        self,
        session_id: str,
        response: str,
        *,
        release: Any | None = None,
        call_log: Path | None = None,
    ) -> None:
        self.session_id = session_id
        self.items = ()
        self.response = response
        self.release = release
        self.call_log = call_log
        self.calls = 0
        self.prompts: list[str] = []
        self.turn_ids: list[str | None] = []

    async def run(
        self,
        prompt: str,
        *,
        turn_id: str | None = None,
        turn_source: TurnSource | None = None,
        **kwargs: object,
    ) -> SimpleNamespace:
        del kwargs
        if turn_source is not None and turn_source.value != "user":
            raise AssertionError("replan must use the user turn source")
        self.calls += 1
        self.prompts.append(prompt)
        self.turn_ids.append(turn_id)
        if self.call_log is not None:
            with self.call_log.open("a", encoding="utf-8") as stream:
                stream.write("call\n")
                stream.flush()
                os.fsync(stream.fileno())
        if self.release is not None:
            await asyncio.to_thread(self.release.wait, 30)
        return SimpleNamespace(response=self.response)


class _BoundaryRunner(_Runner):
    def __init__(self, session_id: str, response: str, mode: str) -> None:
        super().__init__(session_id, response)
        self.mode = mode

    async def run(
        self,
        prompt: str,
        *,
        turn_id: str | None = None,
        turn_source: TurnSource | None = None,
        **kwargs: object,
    ) -> SimpleNamespace:
        del prompt, turn_id, turn_source, kwargs
        self.calls += 1
        if self.mode == "cancel":
            raise asyncio.CancelledError()
        if self.mode == "error":
            raise RuntimeError("fixture replan failure")
        return SimpleNamespace(response=self.response)


class _ReplanProductionProvider(_ProductionPlanningProvider):
    """Fixture provider for composition and process-boundary replan tests."""

    async def stream(
        self,
        context: Any,
        tools: Any,
        *,
        tool_policy: ModelToolPolicy = ModelToolPolicy.ALLOWED,
    ) -> AsyncIterator[ModelEvent]:
        del tool_policy
        contents = "\n".join(message.content for message in context.messages)
        if "bounded DAG Replan Planner" in contents:
            if tools:
                raise AssertionError("Replan Planner received tools")
            self._state.replan_mode = True
            self._state.planner_calls += 1
            self._state.zero_tool_calls += 1
            if self._state.provider_call_log is not None:
                _append_durable_json_line(
                    self._state.provider_call_log,
                    {"response": _REPLAN_PRODUCTION_RESPONSE},
                )
            if self._state.provider_started_event is not None:
                self._state.provider_started_event.set()
                if self._state.provider_release_event is None:
                    raise AssertionError("replan provider release event is missing")
                await asyncio.to_thread(self._state.provider_release_event.wait, 90)
            yield ModelCompleted("stop", response_text=_REPLAN_PRODUCTION_RESPONSE)
            return
        if "Leader decision authority" in contents and "repair-b" in contents:
            if tools:
                raise AssertionError("Replan Leader received tools")
            actions = (
                '{"action":"SELECT_NODES","node_ids":["repair-b","repair-c"]}',
                '{"action":"SELECT_NODE","node_id":"repair-d"}',
                '{"action":"FINALIZE","summary":"replan DAG completed"}',
            )
            if self._state.leader_calls >= len(actions):
                raise AssertionError("Replan Leader fixture received too many decisions")
            self._state.leader_calls += 1
            self._state.zero_tool_calls += 1
            yield ModelCompleted("stop", response_text=actions[self._state.leader_calls - 1])
            return
        async for event in super().stream(context, tools, tool_policy=ModelToolPolicy.ALLOWED):
            yield event


def _binding(runner: _Runner, *, zero_tools: bool = True) -> ConversationBinding:
    capabilities = SubagentCapabilitySet.from_runtime(
        tool_names=() if zero_tools else ("read_file",),
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


class _DagService:
    def __init__(self, store: SqliteSessionStore, parent_session_id: str) -> None:
        self.store = store
        self.parent_session_id = parent_session_id
        self.calls = 0

    async def create_task_dag(self, request: CreateTaskDagRequest) -> TaskDag:
        self.calls += 1
        dag = TaskDag.create(
            dag_id=request.dag_id,
            parent_session_id=self.parent_session_id,
            nodes=request.nodes,
            created_at=datetime.now(UTC),
            max_parallel=request.max_parallel,
        )
        return await self.store.insert_task_dag(dag)


class _NoopDagService:
    async def create_task_dag(self, request: CreateTaskDagRequest) -> TaskDag:
        del request
        raise AssertionError("the test source should be rejected before publication")


async def _store_and_failed_source(
    directory: str,
) -> tuple[SqliteSessionStore, str, str, TaskDag]:
    store = SqliteSessionStore(Path(directory) / "sessions.db")
    await store.initialize()
    parent_id = await store.create_session(directory, "fixture", "fixture-model")
    planner_id = await store.create_session(directory, "fixture", "fixture-model")
    initial = TaskDag.create(
        dag_id="failed-source",
        parent_session_id=parent_id,
        nodes=(TaskDagNode("source-node", 0, "source work"),),
        created_at=datetime.now(UTC),
    )
    await store.insert_task_dag(initial)
    running_node = replace(
        initial.node("source-node"),
        state=TaskDagNodeState.RUNNING,
        generation=1,
        parent_task_id="source-task",
        execution_owner_pid=os.getpid(),
        execution_owner_token="source-owner",
    )
    running = await store.claim_task_dag_node(
        initial.dag_id,
        running_node,
        expected_generation=0,
        expected_state=TaskDagNodeState.READY,
        updated_at=datetime.now(UTC),
    )
    failed_node = replace(
        running.node("source-node"),
        state=TaskDagNodeState.FAILED,
        generation=2,
        parent_task_id=None,
        execution_owner_pid=None,
        execution_owner_token=None,
        error_kind="fixture_failure",
        error_reason="source worker failed safely",
    )
    finished = await store.finish_task_dag_node(
        initial.dag_id,
        failed_node,
        expected_generation=1,
        expected_state=TaskDagNodeState.RUNNING,
        updated_at=datetime.now(UTC),
    )
    failed = replace(
        finished,
        state=TaskDagState.FAILED,
        generation=finished.generation + 1,
        active_node_id=None,
        updated_at=datetime.now(UTC),
    )
    source = await store.compare_and_transition_task_dag(
        failed,
        expected_generation=finished.generation,
        expected_state=TaskDagState.RUNNING,
    )
    return store, parent_id, planner_id, source


async def _seed_failed_production_source(
    store: SqliteSessionStore,
    parent_session_id: str,
    *,
    dag_id: str = "production-failed-source",
) -> TaskDag:
    """Seed a realistic terminal failure without invoking source workers."""

    dag = TaskDag.create(
        dag_id=dag_id,
        parent_session_id=parent_session_id,
        nodes=(
            TaskDagNode("a", 0, "planning-node-a"),
            TaskDagNode("b", 1, "planning-node-b", dependencies=("a",)),
            TaskDagNode("c", 2, "planning-node-c", dependencies=("a",)),
            TaskDagNode("d", 3, "planning-node-d", dependencies=("b", "c")),
        ),
        created_at=datetime.now(UTC),
        max_parallel=2,
    )
    await store.insert_task_dag(dag)
    _mark_dag_failed_for_replan(store.database_path, dag_id)
    source = await store.get_task_dag(dag_id)
    assert source is not None
    return source


def _mark_dag_failed_for_replan(database: Path, dag_id: str) -> None:
    """Create a quiescent source snapshot for an integration fixture."""

    with closing(sqlite3.connect(database)) as connection, connection:
        connection.executemany(
            """
            UPDATE task_dag_nodes
            SET state = ?, generation = 1, parent_task_id = NULL,
                execution_owner_pid = NULL, execution_owner_token = NULL,
                child_session_id = NULL, lease_id = NULL, worktree_id = NULL,
                baseline_checkpoint_id = NULL, relay_id = NULL,
                error_kind = ?, error_reason = ?, response_preview = ?,
                final_workspace_fingerprint = NULL, changed_file_count = NULL
            WHERE dag_id = ? AND node_id = ?
            """,
            (
                (TaskDagNodeState.COMPLETED.value, None, None, "source completed", dag_id, "a"),
                (
                    TaskDagNodeState.FAILED.value,
                    "fixture_failure",
                    "source branch failed",
                    None,
                    dag_id,
                    "b",
                ),
                (
                    TaskDagNodeState.FAILED.value,
                    "fixture_failure",
                    "parallel source branch failed",
                    None,
                    dag_id,
                    "c",
                ),
                (
                    TaskDagNodeState.SKIPPED.value,
                    "blocked_by_failure",
                    "source dependency failed",
                    None,
                    dag_id,
                    "d",
                ),
            ),
        )
        connection.execute(
            """
            UPDATE task_dags
            SET state = ?, generation = 1, active_node_id = NULL
            WHERE dag_id = ?
            """,
            (TaskDagState.FAILED.value, dag_id),
        )


async def _record_real_replan_snapshot(
    application: ApplicationComposition,
    *,
    marker_path: Path,
    revision_id: str,
    provider_call_log: Path,
    stage: str,
) -> None:
    store = cast(SqliteSessionStore, application.store)
    attempt = await store.get_task_dag_replan_attempt(revision_id)
    assert attempt is not None
    proposal = await store.get_task_dag_replan_proposal(revision_id)
    successor = (
        await store.get_task_dag(attempt.successor_dag_id)
        if attempt.successor_dag_id is not None
        else await store.get_task_dag(attempt.intended_successor_dag_id)
    )
    turns = await store.load_turn_attempts(attempt.planner_session_id)
    turn = next((item for item in turns if item.turn_id == attempt.planner_turn_id), None)
    _write_durable_json(
        marker_path,
        {
            "stage": stage,
            "revision_id": attempt.revision_id,
            "parent_session_id": attempt.parent_session_id,
            "planner_session_id": attempt.planner_session_id,
            "planner_turn_id": attempt.planner_turn_id,
            "source_dag_id": attempt.source_dag_id,
            "source_generation": attempt.source_generation,
            "source_definition_fingerprint": attempt.source_definition_fingerprint,
            "evidence_fingerprint": attempt.evidence_fingerprint,
            "intended_successor_dag_id": attempt.intended_successor_dag_id,
            "state": attempt.state.value,
            "model_response": attempt.model_response,
            "proposal_id": proposal.proposal_id if proposal is not None else None,
            "proposal_fingerprint": (
                proposal.proposal_fingerprint if proposal is not None else None
            ),
            "proposal_canonical_json": (
                proposal.proposal.canonical_json if proposal is not None else None
            ),
            "successor_dag_id": successor.dag_id if successor is not None else None,
            "successor_definition_fingerprint": (
                successor.definition_fingerprint if successor is not None else None
            ),
            "provider_call_count": len(_read_durable_json_lines(provider_call_log)),
            "turn_id": turn.turn_id if turn is not None else None,
            "turn_last_stage": turn.last_stage.value if turn is not None else None,
            "turn_request_started_count": (
                turn.request_started_count if turn is not None else None
            ),
            "turn_output_started": turn.output_started if turn is not None else None,
        },
    )


async def _service(
    store: SqliteSessionStore,
    parent_id: str,
    planner_id: str,
    response: str = _REPLAN_RESPONSE,
    *,
    release: Any | None = None,
    call_log: Path | None = None,
    runner: _Runner | None = None,
) -> tuple[TaskDagReplanApplicationService, _Runner]:
    runner = runner or _Runner(planner_id, response, release=release, call_log=call_log)
    parent = _binding(_Runner(parent_id, "unused"), zero_tools=False)
    planner = _binding(runner)
    return (
        TaskDagReplanApplicationService(
            store,
            store,
            _DagService(store, parent_id),
            parent_binding=parent,
            planner_binding=planner,
            session_store=store,
        ),
        runner,
    )


def test_replan_evidence_is_strict_redacted_and_bounded() -> None:
    now = datetime.now(UTC)
    source = TaskDag(
        dag_id="evidence-source",
        parent_session_id="parent",
        nodes=(
            TaskDagNode(
                "a",
                0,
                "completed",
                state=TaskDagNodeState.COMPLETED,
                generation=2,
                response_preview="token=secret-value result",
                changed_file_count=2,
            ),
            TaskDagNode(
                "b",
                1,
                "failed",
                dependencies=("a",),
                state=TaskDagNodeState.FAILED,
                generation=3,
                error_kind="provider_error",
                error_reason="bounded failure detail",
            ),
        ),
        state=TaskDagState.FAILED,
        generation=5,
        created_at=now,
        updated_at=now,
    )
    envelope = TaskDagReplanEvidenceEnvelope(
        source.dag_id,
        source.definition_fingerprint,
        source.state,
        source.generation,
        (
            # The domain itself remains provider-agnostic; redaction is done at
            # the application boundary below.
            DagReplanEvidenceNode(
                "a",
                0,
                TaskDagNodeState.COMPLETED,
                2,
                result_projection="[REDACTED] result",
                changed_file_count=2,
            ),
            DagReplanEvidenceNode(
                "b",
                1,
                TaskDagNodeState.FAILED,
                3,
                dependencies=("a",),
                failure_kind="provider_error",
                failure_summary="bounded failure detail",
            ),
        ),
    )
    assert "secret-value" not in envelope.render()
    assert "worktree" not in envelope.render()
    assert (
        envelope.fingerprint
        == TaskDagReplanEvidenceEnvelope.parse(envelope.canonical_json).fingerprint
    )
    with pytest.raises(ValueError, match="canonical JSON"):
        TaskDagReplanEvidenceEnvelope.parse(json.dumps(json.loads(envelope.canonical_json)))
    with pytest.raises(ValueError, match="result projection"):
        DagReplanEvidenceNode(
            "b",
            1,
            TaskDagNodeState.FAILED,
            0,
            result_projection="not allowed",
        )
    with pytest.raises(ValueError, match="terminal and determinate"):
        TaskDagReplanEvidenceEnvelope(
            source.dag_id,
            source.definition_fingerprint,
            source.state,
            source.generation,
            (DagReplanEvidenceNode("a", 0, TaskDagNodeState.RUNNING, 0),),
        )
    with pytest.raises(ValueError, match="known nodes"):
        TaskDagReplanEvidenceEnvelope(
            source.dag_id,
            source.definition_fingerprint,
            source.state,
            source.generation,
            (
                DagReplanEvidenceNode(
                    "a",
                    0,
                    TaskDagNodeState.COMPLETED,
                    0,
                    dependencies=("missing",),
                ),
            ),
        )
    assert MAX_DAG_REPLAN_COMPLETED_RESULT_BYTES == 4 * 1024


def _valid_domain_evidence() -> TaskDagReplanEvidenceEnvelope:
    return TaskDagReplanEvidenceEnvelope(
        source_dag_id="domain-source",
        source_definition_fingerprint="a" * 64,
        source_terminal_state=TaskDagState.FAILED,
        source_generation=1,
        nodes=(
            DagReplanEvidenceNode(
                node_id="completed",
                ordinal=0,
                state=TaskDagNodeState.COMPLETED,
                generation=1,
                result_projection="completed result",
            ),
            DagReplanEvidenceNode(
                node_id="failed",
                ordinal=1,
                state=TaskDagNodeState.FAILED,
                generation=1,
                dependencies=("completed",),
                failure_kind="fixture_failure",
                failure_summary="failed result",
            ),
        ),
    )


def _valid_domain_attempt(
    *,
    evidence: TaskDagReplanEvidenceEnvelope | None = None,
    **changes: object,
) -> DagReplanAttempt:
    selected = evidence or _valid_domain_evidence()
    now = datetime.now(UTC)
    values: dict[str, Any] = {
        "revision_id": "domain-revision",
        "parent_session_id": "domain-parent",
        "source_dag_id": selected.source_dag_id,
        "source_definition_fingerprint": selected.source_definition_fingerprint,
        "source_generation": selected.source_generation,
        "source_state": selected.source_terminal_state,
        "revision_depth": 1,
        "evidence_fingerprint": selected.fingerprint,
        "evidence_json": selected.canonical_json,
        "planner_session_id": "domain-planner",
        "planner_turn_id": "domain-turn",
        "intended_successor_dag_id": "domain-successor",
        "state": DagReplanAttemptState.CLAIMED,
        "owner_id": "domain-owner",
        "lease_expires_at": now + timedelta(minutes=5),
        "created_at": now,
        "updated_at": now,
    }
    values.update(changes)
    return DagReplanAttempt(**values)


def test_replan_domain_rejects_invalid_contract_values() -> None:
    with pytest.raises(ValueError, match="node id"):
        DagReplanEvidenceNode("", 0, TaskDagNodeState.COMPLETED, 0)
    with pytest.raises(ValueError, match="ordinal"):
        DagReplanEvidenceNode("node", -1, TaskDagNodeState.COMPLETED, 0)
    with pytest.raises(TypeError, match="state must be canonical"):
        DagReplanEvidenceNode("node", 0, cast(Any, "completed"), 0)
    with pytest.raises(ValueError, match="generation"):
        DagReplanEvidenceNode("node", 0, TaskDagNodeState.COMPLETED, -1)
    with pytest.raises(TypeError, match="dependencies must be a tuple"):
        DagReplanEvidenceNode(
            "node", 0, TaskDagNodeState.COMPLETED, 0, dependencies=cast(Any, ["other"])
        )
    with pytest.raises(ValueError, match="too many dependencies"):
        DagReplanEvidenceNode(
            "node",
            0,
            TaskDagNodeState.COMPLETED,
            0,
            dependencies=tuple(f"dependency-{index}" for index in range(100)),
        )
    with pytest.raises(ValueError, match="unique"):
        DagReplanEvidenceNode(
            "node", 0, TaskDagNodeState.COMPLETED, 0, dependencies=("other", "other")
        )
    with pytest.raises(ValueError, match="result projection"):
        DagReplanEvidenceNode(
            "node", 0, TaskDagNodeState.COMPLETED, 0, result_projection="bad\x00value"
        )
    with pytest.raises(TypeError, match="truncated"):
        DagReplanEvidenceNode(
            "node", 0, TaskDagNodeState.COMPLETED, 0, result_truncated=cast(Any, "yes")
        )
    with pytest.raises(ValueError, match="changed file count"):
        DagReplanEvidenceNode("node", 0, TaskDagNodeState.COMPLETED, 0, changed_file_count=-1)
    with pytest.raises(ValueError, match="failure state"):
        DagReplanEvidenceNode("node", 0, TaskDagNodeState.COMPLETED, 0, failure_kind="unexpected")
    with pytest.raises(ValueError, match="result projection"):
        DagReplanEvidenceNode("node", 0, TaskDagNodeState.FAILED, 0, result_projection="unexpected")

    valid = _valid_domain_evidence()
    with pytest.raises(ValueError, match="source state"):
        TaskDagReplanEvidenceEnvelope(
            valid.source_dag_id,
            valid.source_definition_fingerprint,
            TaskDagState.COMPLETED,
            valid.source_generation,
            valid.nodes,
        )
    with pytest.raises(ValueError, match="source generation"):
        TaskDagReplanEvidenceEnvelope(
            valid.source_dag_id,
            valid.source_definition_fingerprint,
            valid.source_terminal_state,
            -1,
            valid.nodes,
        )
    with pytest.raises(ValueError, match="non-empty"):
        TaskDagReplanEvidenceEnvelope(
            valid.source_dag_id,
            valid.source_definition_fingerprint,
            valid.source_terminal_state,
            valid.source_generation,
            (),
        )
    with pytest.raises(TypeError, match="nodes must be canonical"):
        TaskDagReplanEvidenceEnvelope(
            valid.source_dag_id,
            valid.source_definition_fingerprint,
            valid.source_terminal_state,
            valid.source_generation,
            cast(Any, (object(),)),
        )
    with pytest.raises(ValueError, match="canonical order"):
        TaskDagReplanEvidenceEnvelope(
            valid.source_dag_id,
            valid.source_definition_fingerprint,
            valid.source_terminal_state,
            valid.source_generation,
            tuple(reversed(valid.nodes)),
        )
    with pytest.raises(ValueError, match="ordinals"):
        TaskDagReplanEvidenceEnvelope(
            valid.source_dag_id,
            valid.source_definition_fingerprint,
            valid.source_terminal_state,
            valid.source_generation,
            (
                replace(valid.nodes[0], ordinal=1),
                replace(valid.nodes[1], ordinal=2),
            ),
        )
    with pytest.raises(ValueError, match="node ids"):
        TaskDagReplanEvidenceEnvelope(
            valid.source_dag_id,
            valid.source_definition_fingerprint,
            valid.source_terminal_state,
            valid.source_generation,
            (
                valid.nodes[0],
                replace(valid.nodes[1], node_id=valid.nodes[0].node_id, dependencies=()),
            ),
        )
    with pytest.raises(ValueError, match="completed-result"):
        TaskDagReplanEvidenceEnvelope(
            valid.source_dag_id,
            valid.source_definition_fingerprint,
            valid.source_terminal_state,
            valid.source_generation,
            tuple(
                DagReplanEvidenceNode(
                    f"completed-{index}",
                    index,
                    TaskDagNodeState.COMPLETED,
                    0,
                    result_projection="x" * MAX_DAG_REPLAN_COMPLETED_RESULT_BYTES,
                )
                for index in range(5)
            ),
        )
    with pytest.raises(ValueError, match="failure-state"):
        TaskDagReplanEvidenceEnvelope(
            valid.source_dag_id,
            valid.source_definition_fingerprint,
            valid.source_terminal_state,
            valid.source_generation,
            tuple(
                DagReplanEvidenceNode(
                    f"failed-{index}",
                    index,
                    TaskDagNodeState.FAILED,
                    0,
                    failure_summary="x" * 5_000,
                )
                for index in range(2)
            ),
        )
    with pytest.raises(ValueError, match="envelope"):
        TaskDagReplanEvidenceEnvelope(
            valid.source_dag_id,
            valid.source_definition_fingerprint,
            valid.source_terminal_state,
            valid.source_generation,
            tuple(
                DagReplanEvidenceNode(
                    f"large-node-{index}-{'x' * 110}",
                    index,
                    TaskDagNodeState.FAILED,
                    0,
                )
                for index in range(250)
            ),
        )


def test_replan_domain_parser_rejects_malformed_json_shapes() -> None:
    cases: list[dict[str, object]] = []
    cases.append({"unknown": True})

    def replace_nodes(raw: dict[str, Any], value: object) -> None:
        raw["nodes"] = value

    def replace_first_node(raw: dict[str, Any], value: object) -> None:
        nodes = cast(list[object], raw["nodes"])
        nodes[0] = value

    def update_first_node(raw: dict[str, Any], **changes: object) -> None:
        nodes = cast(list[dict[str, object]], raw["nodes"])
        nodes[0].update(changes)

    mutations: tuple[Callable[[dict[str, Any]], None], ...] = (
        lambda raw: replace_nodes(raw, {}),
        lambda raw: replace_first_node(raw, {"node_id": "node"}),
        lambda raw: update_first_node(raw, node_id=1),
        lambda raw: update_first_node(raw, ordinal=True),
        lambda raw: update_first_node(raw, dependencies="bad"),
        lambda raw: update_first_node(raw, state=1),
        lambda raw: update_first_node(raw, result_projection=1),
        lambda raw: update_first_node(raw, result_truncated=1),
        lambda raw: update_first_node(raw, failure_kind=1),
        lambda raw: update_first_node(raw, changed_file_count=True),
        lambda raw: update_first_node(raw, state="not-a-state"),
        lambda raw: raw.update(source_dag_id=1),
        lambda raw: raw.update(source_terminal_state="not-a-state"),
    )
    for mutation in mutations:
        mutated = cast(dict[str, Any], json.loads(_valid_domain_evidence().canonical_json))
        mutation(mutated)
        cases.append(mutated)
    for raw in cases:
        with pytest.raises(ValueError, match=r".+"):
            TaskDagReplanEvidenceEnvelope.parse(json.dumps(raw))
    with pytest.raises(ValueError, match="strict JSON"):
        TaskDagReplanEvidenceEnvelope.parse("not-json")


def test_replan_domain_attempt_and_proposal_invariants_are_enforced() -> None:
    valid = _valid_domain_evidence()
    proposal = ModelDagProposal.parse(_REPLAN_RESPONSE)
    with pytest.raises(ValueError, match="revision id"):
        RunTaskDagReplanRequest("", valid.source_dag_id)
    with pytest.raises(ValueError, match="source DAG id"):
        RunTaskDagReplanRequest("revision", "\x00source")
    attempt_cases = (
        ({"source_generation": -1}, "source generation"),
        ({"source_state": TaskDagState.COMPLETED}, "source state"),
        ({"revision_depth": 0}, "depth"),
        ({"evidence_fingerprint": "b" * 64}, "evidence identity"),
        ({"state": cast(Any, "claimed")}, "state must be canonical"),
        ({"lease_expires_at": datetime.now(UTC).replace(tzinfo=None)}, "lease expiry"),
        ({"model_response": ""}, "model response"),
        ({"proposal_fingerprint": "bad"}, "fingerprint"),
        ({"created_at": datetime.now(UTC).replace(tzinfo=None)}, "created at"),
        (
            {
                "created_at": datetime.now(UTC),
                "updated_at": datetime.now(UTC) - timedelta(seconds=1),
            },
            "updated_at",
        ),
        (
            {"state": DagReplanAttemptState.MODEL_COMMITTED, "model_response": None},
            "durable replan output",
        ),
        (
            {
                "state": DagReplanAttemptState.PROPOSAL_PUBLISHED,
                "model_response": _REPLAN_RESPONSE,
                "proposal_fingerprint": None,
            },
            "proposal identity",
        ),
        (
            {
                "state": DagReplanAttemptState.SUCCESSOR_DAG_PUBLISHED,
                "model_response": _REPLAN_RESPONSE,
                "proposal_fingerprint": "b" * 64,
                "successor_dag_id": None,
            },
            "successor identity",
        ),
    )
    for changes, message in attempt_cases:
        with pytest.raises((TypeError, ValueError), match=message or None):
            _valid_domain_attempt(evidence=valid, **changes)

    with pytest.raises(ValueError, match="source generation"):
        DagReplanProposalRecord(
            "proposal",
            "revision",
            "parent",
            "source",
            "a" * 64,
            -1,
            "b" * 64,
            "successor",
            cast(Any, object()),
            datetime.now(UTC),
        )
    with pytest.raises(TypeError, match="canonical"):
        DagReplanProposalRecord(
            "proposal",
            "revision",
            "parent",
            "source",
            "a" * 64,
            0,
            "b" * 64,
            "successor",
            cast(Any, object()),
            datetime.now(UTC),
        )
    with pytest.raises(ValueError, match="created_at"):
        DagReplanProposalRecord(
            "proposal",
            "revision",
            "parent",
            "source",
            "a" * 64,
            0,
            "b" * 64,
            "successor",
            proposal,
            datetime.now(UTC).replace(tzinfo=None),
        )


@pytest.mark.asyncio
async def test_replan_publishes_distinct_successor_and_is_idempotent() -> None:
    with tempfile.TemporaryDirectory() as directory:
        store, parent_id, planner_id, source_before = await _store_and_failed_source(directory)
        service, runner = await _service(store, parent_id, planner_id)
        result = await service.run(RunTaskDagReplanRequest("revision-1", source_before.dag_id))
        assert result.attempt.state is DagReplanAttemptState.COMPLETED
        assert result.successor_dag.dag_id != source_before.dag_id
        assert result.dag == result.successor_dag
        assert result.successor_dag.parent_session_id == parent_id
        assert result.successor_dag.node("fix-a").state is TaskDagNodeState.READY
        assert runner.calls == 1
        source_after = await store.get_task_dag(source_before.dag_id)
        assert source_after == source_before
        repeated = await service.run(RunTaskDagReplanRequest("revision-1", source_before.dag_id))
        assert repeated.successor_dag == result.successor_dag
        assert repeated.proposal == result.proposal
        assert runner.calls == 1
        assert await store.get_task_dag_replan_source_depth(result.successor_dag.dag_id) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [TaskDagState.READY, TaskDagState.RUNNING, TaskDagState.COMPLETED, TaskDagState.CANCELLED],
)
async def test_replan_rejects_non_failed_source_states(state: TaskDagState) -> None:
    now = datetime.now(UTC)
    source = TaskDag(
        "ineligible",
        "parent",
        (TaskDagNode("a", 0, "work", state=TaskDagNodeState.COMPLETED, generation=1),),
        state=state,
        generation=1,
        created_at=now,
        updated_at=now,
    )

    class DagOnlyStore:
        async def get_task_dag(self, dag_id: str) -> TaskDag | None:
            return source if dag_id == source.dag_id else None

    runner = _Runner("planner", _REPLAN_RESPONSE)
    service = TaskDagReplanApplicationService(
        cast(TaskDagReplanStore, object()),
        cast(TaskDagStore, DagOnlyStore()),
        _NoopDagService(),
        parent_binding=_binding(_Runner("parent", "unused"), zero_tools=False),
        planner_binding=_binding(runner),
    )
    with pytest.raises(ConfigurationError, match="failed source DAG"):
        await service.run(RunTaskDagReplanRequest("revision", source.dag_id))
    assert runner.calls == 0


@pytest.mark.asyncio
async def test_replan_rejects_indeterminate_nodes_and_second_depth() -> None:
    now = datetime.now(UTC)
    source = TaskDag(
        "indeterminate",
        "parent",
        (
            TaskDagNode(
                "a",
                0,
                "work",
                state=TaskDagNodeState.INDETERMINATE,
                generation=1,
            ),
        ),
        state=TaskDagState.FAILED,
        generation=1,
        created_at=now,
        updated_at=now,
    )

    class Store:
        async def get_task_dag_replan_source_depth(self, dag_id: str) -> int:
            return 1

    class DagOnlyStore:
        async def get_task_dag(self, dag_id: str) -> TaskDag | None:
            return source

    runner = _Runner("planner", _REPLAN_RESPONSE)
    parent = _binding(_Runner("parent", "unused"), zero_tools=False)
    service = TaskDagReplanApplicationService(
        cast(TaskDagReplanStore, Store()),
        cast(TaskDagStore, DagOnlyStore()),
        _NoopDagService(),
        parent_binding=parent,
        planner_binding=_binding(runner),
    )
    with pytest.raises(ConfigurationError, match="indeterminate"):
        await service.run(RunTaskDagReplanRequest("revision", source.dag_id))
    assert runner.calls == 0


@pytest.mark.asyncio
async def test_replan_rejects_a_quiescent_source_at_the_depth_boundary() -> None:
    now = datetime.now(UTC)
    source = TaskDag(
        "already-revised",
        "parent",
        (
            TaskDagNode("a", 0, "completed", state=TaskDagNodeState.COMPLETED, generation=1),
            TaskDagNode("b", 1, "failed", state=TaskDagNodeState.FAILED, generation=1),
        ),
        state=TaskDagState.FAILED,
        generation=1,
        created_at=now,
        updated_at=now,
    )

    class Store:
        async def get_task_dag_replan_source_depth(self, dag_id: str) -> int:
            del dag_id
            return 1

    class DagOnlyStore:
        async def get_task_dag(self, dag_id: str) -> TaskDag | None:
            return source if dag_id == source.dag_id else None

    runner = _Runner("planner", _REPLAN_RESPONSE)
    service = TaskDagReplanApplicationService(
        cast(TaskDagReplanStore, Store()),
        cast(TaskDagStore, DagOnlyStore()),
        _NoopDagService(),
        parent_binding=_binding(_Runner("parent", "unused"), zero_tools=False),
        planner_binding=_binding(runner),
    )
    with pytest.raises(ConfigurationError, match="depth limit"):
        await service.run(RunTaskDagReplanRequest("revision", source.dag_id))
    assert runner.calls == 0


@pytest.mark.asyncio
async def test_replan_model_binding_is_exactly_zero_tool_one_step() -> None:
    with tempfile.TemporaryDirectory() as directory:
        store, parent_id, planner_id, _source = await _store_and_failed_source(directory)
        with pytest.raises(ConfigurationError, match="zero tools"):
            TaskDagReplanApplicationService(
                store,
                store,
                _DagService(store, parent_id),
                parent_binding=_binding(_Runner(parent_id, "unused"), zero_tools=False),
                planner_binding=_binding(_Runner(planner_id, _REPLAN_RESPONSE), zero_tools=False),
            )
        with pytest.raises(ConfigurationError, match="lease duration"):
            TaskDagReplanApplicationService(
                store,
                store,
                _DagService(store, parent_id),
                parent_binding=_binding(_Runner(parent_id, "unused"), zero_tools=False),
                planner_binding=_binding(_Runner(planner_id, _REPLAN_RESPONSE)),
                lease_seconds=MAX_DAG_REPLAN_LEASE_SECONDS + 1,
            )


@pytest.mark.asyncio
async def test_replan_application_constructor_rejects_identity_and_authority_gaps() -> None:
    with tempfile.TemporaryDirectory() as directory:
        store, parent_id, planner_id, _source = await _store_and_failed_source(directory)
        parent = _binding(_Runner(parent_id, "unused"), zero_tools=False)
        planner = _binding(_Runner(planner_id, _REPLAN_RESPONSE))
        with pytest.raises(ConfigurationError, match="parent binding"):
            TaskDagReplanApplicationService(
                store,
                store,
                _NoopDagService(),
                parent_binding=cast(Any, object()),
                planner_binding=planner,
            )
        with pytest.raises(ConfigurationError, match="model binding"):
            TaskDagReplanApplicationService(
                store,
                store,
                _NoopDagService(),
                parent_binding=parent,
                planner_binding=cast(Any, object()),
            )
        with pytest.raises(ConfigurationError, match="Task DAG service"):
            TaskDagReplanApplicationService(
                store,
                store,
                cast(Any, object()),
                parent_binding=parent,
                planner_binding=planner,
            )
        with pytest.raises(ConfigurationError, match="parent session identity"):
            TaskDagReplanApplicationService(
                store,
                store,
                _NoopDagService(),
                parent_binding=_binding(_Runner("", "unused"), zero_tools=False),
                planner_binding=planner,
            )
        with pytest.raises(ConfigurationError, match="model session identity"):
            TaskDagReplanApplicationService(
                store,
                store,
                _NoopDagService(),
                parent_binding=parent,
                planner_binding=_binding(_Runner("", _REPLAN_RESPONSE)),
            )
        with pytest.raises(ConfigurationError, match="fresh session"):
            TaskDagReplanApplicationService(
                store,
                store,
                _NoopDagService(),
                parent_binding=parent,
                planner_binding=_binding(_Runner(parent_id, _REPLAN_RESPONSE)),
            )
        with pytest.raises(ConfigurationError, match="owner identity"):
            TaskDagReplanApplicationService(
                store,
                store,
                _NoopDagService(),
                parent_binding=parent,
                planner_binding=planner,
                owner_id="",
            )


@pytest.mark.asyncio
async def test_replan_model_failures_are_durable_and_never_replayed() -> None:
    async def run_case(
        revision_id: str,
        mode: str,
        response: str,
        message: str | None,
        expected_state: DagReplanAttemptState,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store, parent_id, _planner_id, source = await _store_and_failed_source(directory)
            planner_id = await store.create_session(directory, "fixture", "fixture-model")
            runner = _BoundaryRunner(planner_id, response, mode)
            service, _ = await _service(
                store,
                parent_id,
                planner_id,
                runner=runner,
            )
            with pytest.raises((ConfigurationError, asyncio.CancelledError), match=message):
                await service.run(RunTaskDagReplanRequest(revision_id, source.dag_id))
            assert runner.calls == 1
            attempt = await store.get_task_dag_replan_attempt(revision_id)
            assert attempt is not None
            assert attempt.state is expected_state

    await run_case(
        "model-error",
        "error",
        _REPLAN_RESPONSE,
        "model turn failed",
        DagReplanAttemptState.INDETERMINATE,
    )
    await run_case(
        "model-cancelled",
        "cancel",
        _REPLAN_RESPONSE,
        None,
        DagReplanAttemptState.INDETERMINATE,
    )
    await run_case(
        "empty-response",
        "response",
        "",
        "empty response",
        DagReplanAttemptState.INDETERMINATE,
    )
    await run_case(
        "oversized-response",
        "response",
        "x" * (16 * 1024 + 1),
        "bounded response",
        DagReplanAttemptState.STALE,
    )

    with tempfile.TemporaryDirectory() as directory:
        store, parent_id, planner_id, source = await _store_and_failed_source(directory)
        service, runner = await _service(store, parent_id, planner_id)

        async def fail_commit(*args: Any, **kwargs: Any) -> Any:
            del args, kwargs
            raise TaskDagReplanStoreError("fixture commit failure")

        with (
            patch.object(
                store,
                "mark_task_dag_replan_model_committed",
                new=fail_commit,
            ),
            pytest.raises(ConfigurationError, match="output durability"),
        ):
            await service.run(RunTaskDagReplanRequest("commit-error", source.dag_id))
        assert runner.calls == 1
        attempt = await store.get_task_dag_replan_attempt("commit-error")
        assert attempt is not None
        assert attempt.state is DagReplanAttemptState.PROVIDER_FENCED


@pytest.mark.asyncio
async def test_replan_application_guards_fail_closed_at_private_boundaries() -> None:
    with tempfile.TemporaryDirectory() as directory:
        store, parent_id, planner_id, source = await _store_and_failed_source(directory)
        service, _runner = await _service(store, parent_id, planner_id)
        evidence = service._build_evidence(source)
        valid_attempt = _valid_domain_attempt(evidence=evidence)
        now = datetime.now(UTC)

        with pytest.raises(ValueError, match="canonical"):
            await service.run(cast(Any, object()))

        async def missing_dag(_dag_id: str) -> None:
            return None

        async def broken_dag(_dag_id: str) -> TaskDag:
            raise RuntimeError("fixture DAG lookup failure")

        with (
            patch.object(store, "get_task_dag", new=missing_dag),
            pytest.raises(ConfigurationError, match="source DAG is missing"),
        ):
            await service._load_source("missing")
        with (
            patch.object(store, "get_task_dag", new=broken_dag),
            pytest.raises(ConfigurationError, match="source DAG lookup failed"),
        ):
            await service._load_source("broken")

        async def broken_depth(_dag_id: str) -> int:
            raise TaskDagReplanStoreError("fixture depth failure")

        async def invalid_depth(_dag_id: str) -> int:
            return 2

        with (
            patch.object(store, "get_task_dag_replan_source_depth", new=broken_depth),
            pytest.raises(ConfigurationError, match="depth lookup failed"),
        ):
            await service._source_depth(source.dag_id)
        with (
            patch.object(store, "get_task_dag_replan_source_depth", new=invalid_depth),
            pytest.raises(ConfigurationError, match="outside"),
        ):
            await service._source_depth(source.dag_id)

        async def broken_proposal(_revision_id: str) -> None:
            raise TaskDagReplanStoreError("fixture proposal lookup failure")

        with (
            patch.object(store, "get_task_dag_replan_proposal", new=broken_proposal),
            pytest.raises(ConfigurationError, match="proposal lookup failed"),
        ):
            await service._load_proposal("revision")

        async def broken_claim(*args: Any, **kwargs: Any) -> Any:
            del args, kwargs
            raise TaskDagReplanStoreError("fixture claim failure")

        async def broken_fence(*args: Any, **kwargs: Any) -> Any:
            del args, kwargs
            raise TaskDagReplanStoreError("fixture fence failure")

        with (
            patch.object(store, "claim_task_dag_replan_attempt", new=broken_claim),
            pytest.raises(ConfigurationError, match="durable claim failed"),
        ):
            await service._claim(valid_attempt, now)
        with (
            patch.object(store, "fence_task_dag_replan_attempt", new=broken_fence),
            pytest.raises(ConfigurationError, match="provider fence was lost"),
        ):
            await service._fence(valid_attempt)

        with pytest.raises(ConfigurationError, match="another DAG replan controller"):
            await service._recover(valid_attempt, evidence)
        with pytest.raises(ConfigurationError, match="explicit recovery"):
            await service._recover(
                replace(valid_attempt, state=DagReplanAttemptState.STALE), evidence
            )
        with pytest.raises(ConfigurationError, match="no model response"):
            await service._publish_from_model(
                cast(DagReplanAttempt, SimpleNamespace(model_response=None)), evidence
            )

        async def no_proposal(_revision_id: str) -> None:
            return None

        with (
            patch.object(store, "get_task_dag_replan_proposal", new=no_proposal),
            pytest.raises(ConfigurationError, match="proposal is missing"),
        ):
            await service._publish_from_proposal(valid_attempt, evidence)

        with pytest.raises(ConfigurationError, match="owns this attempt"):
            await service._guard_existing_attempt(valid_attempt, now)
        with pytest.raises(ConfigurationError, match="provider fence exists"):
            await service._guard_existing_attempt(
                replace(valid_attempt, state=DagReplanAttemptState.PROVIDER_FENCED), now
            )
        service._session_store = None
        with pytest.raises(ConfigurationError, match="inspection is unavailable"):
            await service._guard_existing_attempt(
                replace(valid_attempt, lease_expires_at=now - timedelta(seconds=1)), now
            )

        class BrokenSessionStore:
            async def load_turn_attempts(self, session_id: str) -> tuple[Any, ...]:
                del session_id
                raise RuntimeError("fixture recovery inspection failure")

        service._session_store = cast(SessionStore, BrokenSessionStore())
        with pytest.raises(ConfigurationError, match="inspection failed"):
            await service._guard_existing_attempt(
                replace(valid_attempt, lease_expires_at=now - timedelta(seconds=1)), now
            )

        await service._mark_stale(replace(valid_attempt, state=DagReplanAttemptState.STALE))
        await service._mark_indeterminate(
            replace(valid_attempt, state=DagReplanAttemptState.INDETERMINATE)
        )

        async def broken_transition(*args: Any, **kwargs: Any) -> Any:
            del args, kwargs
            raise TaskDagReplanStoreError("fixture transition failure")

        with patch.object(store, "transition_task_dag_replan_attempt", new=broken_transition):
            await service._mark_stale(valid_attempt)
            await service._mark_indeterminate(valid_attempt)

        parent_runner = cast(_Runner, service._parent_binding.runner)
        parent_runner.session_id = ""
        with pytest.raises(ConfigurationError, match="session identity is missing"):
            service._require_parent_session_id()
        parent_runner.session_id = "other-parent"
        with pytest.raises(ConfigurationError, match="identity changed"):
            service._require_parent_session_id()
        parent_runner.session_id = parent_id

        with pytest.raises(ConfigurationError, match="outside"):
            service._verify_source_eligibility(source, "other-parent")
        running = SimpleNamespace(
            parent_session_id=parent_id,
            state=TaskDagState.FAILED,
            running_node_ids=("source-node",),
            nodes=(),
        )
        with pytest.raises(ConfigurationError, match="quiescent"):
            service._verify_source_eligibility(cast(Any, running), parent_id)
        nonterminal = SimpleNamespace(
            parent_session_id=parent_id,
            state=TaskDagState.FAILED,
            running_node_ids=(),
            nodes=(SimpleNamespace(state=TaskDagNodeState.PENDING),),
        )
        with pytest.raises(ConfigurationError, match="terminal"):
            service._verify_source_eligibility(cast(Any, nonterminal), parent_id)

        long_completed = replace(
            source,
            nodes=(
                replace(
                    source.nodes[0],
                    state=TaskDagNodeState.COMPLETED,
                    response_preview="x" * 8_000,
                    error_kind=None,
                    error_reason=None,
                ),
            ),
        )
        long_evidence = service._build_evidence(long_completed)
        assert long_evidence.nodes[0].result_truncated is True
        fallback = replace(
            source,
            nodes=(replace(source.nodes[0], error_kind=None, error_reason=None),),
        )
        assert service._build_evidence(fallback).nodes[0].failure_kind == "failed"
        assert service._redacted_projection(None) is None
        assert service._redacted_projection("   ") is None
        assert (
            service._same_source_snapshot(
                replace(source, generation=source.generation + 1), valid_attempt
            )
            is False
        )

        result = await service.run(RunTaskDagReplanRequest("guard-identity", source.dag_id))
        with pytest.raises(ConfigurationError, match="identity conflicts"):
            service._verify_attempt_identity(
                replace(result.attempt, parent_session_id="other-parent"),
                parent_session_id=parent_id,
                source=source,
                evidence=result.evidence,
                revision_depth=result.attempt.revision_depth,
            )
        with pytest.raises(ConfigurationError, match="proposal identity"):
            service._verify_proposal_identity(
                replace(result.proposal, intended_successor_dag_id="other-successor"),
                result.attempt,
                result.evidence,
            )
        with pytest.raises(ConfigurationError, match="distinct"):
            service._verify_successor(
                replace(result.successor_dag, dag_id=result.attempt.source_dag_id),
                result.attempt,
                result.proposal,
            )
        with pytest.raises(ConfigurationError, match="does not match"):
            service._verify_successor(
                replace(result.successor_dag, state=TaskDagState.COMPLETED),
                result.attempt,
                result.proposal,
            )
        with pytest.raises(ConfigurationError, match="prompt exceeds"):
            service._prompt(
                cast(Any, SimpleNamespace(render=lambda: "x" * MAX_DAG_REPLAN_PROMPT_BYTES))
            )


@pytest.mark.asyncio
async def test_replan_recovery_reuses_model_commit_without_provider_replay() -> None:
    with tempfile.TemporaryDirectory() as directory:
        store, parent_id, planner_id, source = await _store_and_failed_source(directory)
        service, first_runner = await _service(store, parent_id, planner_id)
        with (
            patch.object(
                store,
                "publish_task_dag_replan_proposal",
                side_effect=SystemExit("crash after model commit"),
            ),
            pytest.raises(SystemExit),
        ):
            await service.run(RunTaskDagReplanRequest("commit-recovery", source.dag_id))
        committed = await store.get_task_dag_replan_attempt("commit-recovery")
        assert committed is not None
        assert committed.state is DagReplanAttemptState.MODEL_COMMITTED
        assert first_runner.calls == 1
        recovery_planner_id = await store.create_session(directory, "fixture", "fixture-model")
        recovery, second_runner = await _service(store, parent_id, recovery_planner_id)
        recovered = await recovery.run(RunTaskDagReplanRequest("commit-recovery", source.dag_id))
        assert recovered.attempt.state is DagReplanAttemptState.COMPLETED
        assert second_runner.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["proposal", "successor"])
async def test_replan_publication_crash_stages_do_not_replay_provider(stage: str) -> None:
    with tempfile.TemporaryDirectory() as directory:
        store, parent_id, planner_id, source = await _store_and_failed_source(directory)
        if stage == "proposal":
            service, first_runner = await _service(store, parent_id, planner_id)
            original = store.publish_task_dag_replan_proposal

            async def crash_after_proposal(proposal: DagReplanProposalRecord) -> None:
                persisted = await original(proposal)
                del persisted
                raise SystemExit("crash after proposal")

            with (
                patch.object(store, "publish_task_dag_replan_proposal", new=crash_after_proposal),
                pytest.raises(SystemExit),
            ):
                await service.run(RunTaskDagReplanRequest("publication-recovery", source.dag_id))
        else:
            service, first_runner = await _service(store, parent_id, planner_id)
            original_successor = store.mark_task_dag_replan_successor_published

            async def crash_after_successor(
                revision_id: str,
                *,
                successor_dag_id: str,
                proposal_fingerprint: str,
                updated_at: datetime,
            ) -> DagReplanAttempt:
                persisted = await original_successor(
                    revision_id,
                    successor_dag_id=successor_dag_id,
                    proposal_fingerprint=proposal_fingerprint,
                    updated_at=updated_at,
                )
                del persisted
                raise SystemExit("crash after successor")

            with (
                patch.object(
                    store,
                    "mark_task_dag_replan_successor_published",
                    new=crash_after_successor,
                ),
                pytest.raises(SystemExit),
            ):
                await service.run(RunTaskDagReplanRequest("publication-recovery", source.dag_id))
        assert first_runner.calls == 1
        recovery_id = await store.create_session(directory, "fixture", "fixture-model")
        recovery, second_runner = await _service(store, parent_id, recovery_id)
        recovered = await recovery.run(
            RunTaskDagReplanRequest("publication-recovery", source.dag_id)
        )
        assert recovered.attempt.state is DagReplanAttemptState.COMPLETED
        assert second_runner.calls == 0


@pytest.mark.asyncio
async def test_invalid_observable_replan_output_is_stale_and_never_replayed() -> None:
    with tempfile.TemporaryDirectory() as directory:
        store, parent_id, planner_id, source = await _store_and_failed_source(directory)
        invalid = json.dumps(
            {
                "nodes": [],
                "max_parallel": 1,
                "reason": "invalid empty successor",
            }
        )
        service, first_runner = await _service(store, parent_id, planner_id, response=invalid)
        with pytest.raises(ConfigurationError, match="valid immutable successor proposal"):
            await service.run(RunTaskDagReplanRequest("invalid-output", source.dag_id))
        assert first_runner.calls == 1
        attempt = await store.get_task_dag_replan_attempt("invalid-output")
        assert attempt is not None
        assert attempt.state is DagReplanAttemptState.STALE
        assert await store.get_task_dag_replan_proposal("invalid-output") is None

        recovery_planner_id = await store.create_session(directory, "fixture", "fixture-model")
        recovery, second_runner = await _service(store, parent_id, recovery_planner_id)
        with pytest.raises(ConfigurationError, match="explicit recovery"):
            await recovery.run(RunTaskDagReplanRequest("invalid-output", source.dag_id))
        assert second_runner.calls == 0


@pytest.mark.asyncio
async def test_tampered_durable_evidence_is_rejected_before_provider_or_successor() -> None:
    with tempfile.TemporaryDirectory() as directory:
        store, parent_id, planner_id, source = await _store_and_failed_source(directory)
        service, first_runner = await _service(store, parent_id, planner_id)
        first = await service.run(RunTaskDagReplanRequest("tampered-evidence", source.dag_id))
        assert first_runner.calls == 1
        with closing(sqlite3.connect(store.database_path)) as connection, connection:
            connection.execute(
                """
                UPDATE orchestration_dag_replan_attempts
                SET evidence_json = ?
                WHERE revision_id = ?
                """,
                ("{}", "tampered-evidence"),
            )

        recovery_planner_id = await store.create_session(directory, "fixture", "fixture-model")
        recovery, second_runner = await _service(store, parent_id, recovery_planner_id)
        recovery_dag_service = cast(_DagService, recovery._dag_service)
        with pytest.raises(ConfigurationError, match="durable lookup failed"):
            await recovery.run(RunTaskDagReplanRequest("tampered-evidence", source.dag_id))
        assert second_runner.calls == 0
        assert recovery_dag_service.calls == 0
        assert await store.get_task_dag(source.dag_id) == source
        assert await store.get_task_dag(first.successor_dag.dag_id) == first.successor_dag


@pytest.mark.asyncio
async def test_replan_provider_turn_evidence_becomes_indeterminate_without_replay() -> None:
    with tempfile.TemporaryDirectory() as directory:
        store, parent_id, planner_id, source = await _store_and_failed_source(directory)
        service, runner = await _service(store, parent_id, planner_id)
        now = datetime.now(UTC)
        evidence = service._build_evidence(source)
        attempt = DagReplanAttempt(
            "provider-evidence",
            parent_id,
            source.dag_id,
            source.definition_fingerprint,
            source.generation,
            source.state,
            1,
            evidence.fingerprint,
            evidence.canonical_json,
            planner_id,
            "provider-evidence-turn",
            "provider-evidence-successor",
            DagReplanAttemptState.CLAIMED,
            "provider-owner",
            now + timedelta(minutes=5),
        )
        await store.claim_task_dag_replan_attempt(attempt, now=now)
        await store.fence_task_dag_replan_attempt(
            attempt.revision_id,
            owner_id=attempt.owner_id,
            planner_session_id=planner_id,
            planner_turn_id=attempt.planner_turn_id,
            source_dag_id=source.dag_id,
            source_definition_fingerprint=source.definition_fingerprint,
            source_generation=source.generation,
            source_state=source.state.value,
            evidence_fingerprint=evidence.fingerprint,
            updated_at=now,
        )
        await store.start_turn_attempt(
            TurnRecoveryAttempt.create(
                turn_id=attempt.planner_turn_id,
                session_id=planner_id,
                input=TurnInput("replan", source=TurnSource.USER),
                accepted_at=now,
            )
        )
        with pytest.raises(ConfigurationError, match="observable provider-turn evidence"):
            await service.run(RunTaskDagReplanRequest(attempt.revision_id, source.dag_id))
        assert runner.calls == 0
        persisted = await store.get_task_dag_replan_attempt(attempt.revision_id)
        assert persisted is not None
        assert persisted.state is DagReplanAttemptState.INDETERMINATE


@pytest.mark.asyncio
async def test_replan_source_snapshot_change_fails_closed_before_successor() -> None:
    with tempfile.TemporaryDirectory() as directory:
        store, parent_id, planner_id, source = await _store_and_failed_source(directory)
        service, runner = await _service(store, parent_id, planner_id)
        original_get = store.get_task_dag
        lookup_count = 0

        async def changed_after_initial_lookup(dag_id: str) -> TaskDag | None:
            nonlocal lookup_count
            lookup_count += 1
            if lookup_count >= 2:
                return replace(source, generation=source.generation + 1)
            return await original_get(dag_id)

        with (
            patch.object(store, "get_task_dag", new=changed_after_initial_lookup),
            pytest.raises(ConfigurationError, match="snapshot changed"),
        ):
            await service.run(RunTaskDagReplanRequest("changed-source", source.dag_id))
        assert runner.calls == 1
        attempt = await store.get_task_dag_replan_attempt("changed-source")
        assert attempt is not None
        assert attempt.state is DagReplanAttemptState.STALE
        assert await store.get_task_dag_replan_proposal("changed-source") is not None
        assert await store.get_task_dag(attempt.intended_successor_dag_id) is None


def _spawn_replan_race(
    database: str,
    parent_id: str,
    planner_id: str,
    revision_id: str,
    barrier: Any,
    release: Any,
    call_log: str,
    queue: Any,
) -> None:
    async def run() -> None:
        store = SqliteSessionStore(Path(database))
        await store.initialize()
        planner = _Runner(planner_id, _REPLAN_RESPONSE, release=release, call_log=Path(call_log))
        service = TaskDagReplanApplicationService(
            store,
            store,
            _DagService(store, parent_id),
            parent_binding=_binding(_Runner(parent_id, "unused"), zero_tools=False),
            planner_binding=_binding(planner),
            session_store=store,
        )
        await asyncio.to_thread(barrier.wait)
        try:
            result = await service.run(RunTaskDagReplanRequest(revision_id, "failed-source"))
            queue.put(("completed", result.attempt.state.value, planner.calls))
        except Exception as error:
            queue.put(("error", type(error).__name__, planner.calls))

    asyncio.run(run())


def _spawn_real_replan_crash(
    root_directory: str,
    repository_directory: str,
    state_directory: str,
    parent_session_id: str,
    revision_id: str,
    marker: str,
    provider_call_log: str,
    stage: str,
) -> None:
    async def run() -> None:
        root = Path(root_directory)
        repository = Path(repository_directory)
        state_dir = Path(state_directory)
        marker_path = Path(marker)
        call_log = Path(provider_call_log)
        state = _ProductionPlanningState(provider_call_log=call_log)

        def provider_factory(_config: Any, _failover: bool) -> ModelProvider:
            return cast(ModelProvider, _ReplanProductionProvider(state))

        application: ApplicationComposition | None = None
        parent_binding: ConversationBinding | None = None
        replan: TaskDagReplanApplicationService | None = None

        async def close_before_hard_exit(code: int) -> NoReturn:
            """Release fixture resources after the durable crash boundary."""

            if replan is not None:
                await replan.close()
            if parent_binding is not None:
                await parent_binding.close()
            if application is not None:
                await application.close()
            os._exit(code)

        with patch.dict(
            "os.environ",
            _production_planning_environment(root, state_dir),
            clear=False,
        ):
            try:
                application = await ApplicationComposition.open(
                    _production_planning_settings(repository),
                    provider_factory=provider_factory,
                )
                store = cast(SqliteSessionStore, application.store)
                parent_binding = await application.create_binding(
                    resume_id=parent_session_id,
                    capabilities=_parent_capability(repository),
                )
                replan = await application.create_task_dag_replan_service(
                    parent_binding=parent_binding,
                )
                if stage == "model-committed":
                    original_commit = store.mark_task_dag_replan_model_committed

                    async def crash_after_commit(
                        current_revision_id: str,
                        *,
                        owner_id: str,
                        planner_session_id: str,
                        planner_turn_id: str,
                        model_response: str,
                        updated_at: datetime,
                    ) -> DagReplanAttempt:
                        await original_commit(
                            current_revision_id,
                            owner_id=owner_id,
                            planner_session_id=planner_session_id,
                            planner_turn_id=planner_turn_id,
                            model_response=model_response,
                            updated_at=updated_at,
                        )
                        await _record_real_replan_snapshot(
                            application,
                            marker_path=marker_path,
                            revision_id=revision_id,
                            provider_call_log=call_log,
                            stage=stage,
                        )
                        await close_before_hard_exit(73)

                    with patch.object(
                        store,
                        "mark_task_dag_replan_model_committed",
                        new=crash_after_commit,
                    ):
                        await replan.run(
                            RunTaskDagReplanRequest(revision_id, "production-failed-source")
                        )
                elif stage == "proposal-published":
                    original_proposal = store.publish_task_dag_replan_proposal

                    async def crash_after_proposal(
                        proposal: DagReplanProposalRecord,
                    ) -> DagReplanProposalRecord:
                        await original_proposal(proposal)
                        await _record_real_replan_snapshot(
                            application,
                            marker_path=marker_path,
                            revision_id=revision_id,
                            provider_call_log=call_log,
                            stage=stage,
                        )
                        await close_before_hard_exit(74)

                    with patch.object(
                        store,
                        "publish_task_dag_replan_proposal",
                        new=crash_after_proposal,
                    ):
                        await replan.run(
                            RunTaskDagReplanRequest(revision_id, "production-failed-source")
                        )
                elif stage == "successor-dag-inserted":
                    original_insert = store.insert_task_dag

                    async def crash_after_insert(dag: TaskDag) -> TaskDag:
                        await original_insert(dag)
                        await _record_real_replan_snapshot(
                            application,
                            marker_path=marker_path,
                            revision_id=revision_id,
                            provider_call_log=call_log,
                            stage=stage,
                        )
                        await close_before_hard_exit(75)

                    with patch.object(
                        store,
                        "insert_task_dag",
                        new=crash_after_insert,
                    ):
                        await replan.run(
                            RunTaskDagReplanRequest(revision_id, "production-failed-source")
                        )
                elif stage == "provider-turn-evidence":
                    original_fact = store.append_turn_recovery_fact

                    async def crash_after_turn_evidence(
                        session_id: str,
                        turn_id: str,
                        event: Any,
                        fact: Any,
                    ) -> None:
                        await original_fact(session_id, turn_id, event, fact)
                        if fact.kind.value == "model_output_started":
                            await _record_real_replan_snapshot(
                                application,
                                marker_path=marker_path,
                                revision_id=revision_id,
                                provider_call_log=call_log,
                                stage=stage,
                            )
                            await close_before_hard_exit(76)

                    with patch.object(
                        store,
                        "append_turn_recovery_fact",
                        new=crash_after_turn_evidence,
                    ):
                        await replan.run(
                            RunTaskDagReplanRequest(revision_id, "production-failed-source")
                        )
                else:
                    raise AssertionError(f"unknown real replan crash stage: {stage}")
                raise AssertionError(f"real replan did not crash at stage: {stage}")
            finally:
                if replan is not None:
                    await replan.close()
                if parent_binding is not None:
                    await parent_binding.close()
                if application is not None:
                    await application.close()

    asyncio.run(run())


def _spawn_real_replan_recovery(
    root_directory: str,
    repository_directory: str,
    state_directory: str,
    parent_session_id: str,
    revision_id: str,
    marker: str,
    provider_call_log: str,
    result_path: str,
) -> None:
    async def run() -> None:
        root = Path(root_directory)
        repository = Path(repository_directory)
        state_dir = Path(state_directory)
        call_log = Path(provider_call_log)
        result_file = Path(result_path)
        state = _ProductionPlanningState(provider_call_log=call_log)

        def provider_factory(_config: Any, _failover: bool) -> ModelProvider:
            return cast(ModelProvider, _ReplanProductionProvider(state))

        payload: dict[str, object] = {
            "planner_session_id": None,
            "status": "error",
            "provider_call_count_before": len(_read_durable_json_lines(call_log)),
        }
        application: ApplicationComposition | None = None
        parent_binding: ConversationBinding | None = None
        replan: TaskDagReplanApplicationService | None = None
        with patch.dict(
            "os.environ",
            _production_planning_environment(root, state_dir),
            clear=False,
        ):
            try:
                application = await ApplicationComposition.open(
                    _production_planning_settings(repository),
                    provider_factory=provider_factory,
                )
                parent_binding = await application.create_binding(
                    resume_id=parent_session_id,
                    capabilities=_parent_capability(repository),
                )
                replan = await application.create_task_dag_replan_service(
                    parent_binding=parent_binding,
                )
                payload["planner_session_id"] = replan.replan_session_id
                try:
                    result = await replan.run(
                        RunTaskDagReplanRequest(revision_id, "production-failed-source")
                    )
                except Exception as error:
                    payload.update(
                        {
                            "status": "error",
                            "error_type": type(error).__name__,
                            "error_message": str(error),
                        }
                    )
                else:
                    payload.update(
                        {
                            "status": "completed",
                            "attempt_state": result.attempt.state.value,
                            "historical_planner_session_id": result.attempt.planner_session_id,
                            "historical_planner_turn_id": result.attempt.planner_turn_id,
                            "source_dag_id": result.attempt.source_dag_id,
                            "intended_successor_dag_id": result.attempt.intended_successor_dag_id,
                            "successor_dag_id": result.successor_dag.dag_id,
                            "successor_definition_fingerprint": (
                                result.successor_dag.definition_fingerprint
                            ),
                            "proposal_id": result.proposal.proposal_id,
                            "proposal_fingerprint": result.proposal.proposal_fingerprint,
                            "proposal_canonical_json": result.proposal.proposal.canonical_json,
                        }
                    )
            except Exception as error:
                payload.update(
                    {
                        "status": "error",
                        "error_type": type(error).__name__,
                        "error_message": str(error),
                    }
                )
            finally:
                if replan is not None:
                    await replan.close()
                if parent_binding is not None:
                    await parent_binding.close()
                if application is not None:
                    await application.close()
        payload["provider_call_count_after"] = len(_read_durable_json_lines(call_log))
        attempt = await SqliteSessionStore(state_dir / "sessions.db").get_task_dag_replan_attempt(
            revision_id
        )
        payload["durable_attempt_state"] = attempt.state.value if attempt is not None else None
        _write_durable_json(result_file, payload)

    asyncio.run(run())


def _spawn_real_replan_controller(
    root_directory: str,
    repository_directory: str,
    state_directory: str,
    parent_session_id: str,
    revision_id: str,
    provider_call_log: str,
    start_barrier: Any,
    provider_started_event: Any,
    provider_release_event: Any,
    controller_finished_event: Any,
    result_path: str,
) -> None:
    async def run() -> None:
        root = Path(root_directory)
        repository = Path(repository_directory)
        state_dir = Path(state_directory)
        call_log = Path(provider_call_log)
        result_file = Path(result_path)
        state = _ProductionPlanningState(
            provider_call_log=call_log,
            provider_started_event=provider_started_event,
            provider_release_event=provider_release_event,
        )

        def provider_factory(_config: Any, _failover: bool) -> ModelProvider:
            return cast(ModelProvider, _ReplanProductionProvider(state))

        payload: dict[str, object] = {"status": "error"}
        application: ApplicationComposition | None = None
        parent_binding: ConversationBinding | None = None
        replan: TaskDagReplanApplicationService | None = None
        try:
            with patch.dict(
                "os.environ",
                _production_planning_environment(root, state_dir),
                clear=False,
            ):
                application = await ApplicationComposition.open(
                    _production_planning_settings(repository),
                    provider_factory=provider_factory,
                )
                parent_binding = await application.create_binding(
                    resume_id=parent_session_id,
                    capabilities=_parent_capability(repository),
                )
                replan = await application.create_task_dag_replan_service(
                    parent_binding=parent_binding,
                )
                payload["planner_session_id"] = replan.replan_session_id
                await asyncio.to_thread(start_barrier.wait, 90)
                try:
                    result = await replan.run(
                        RunTaskDagReplanRequest(revision_id, "production-failed-source")
                    )
                except Exception as error:
                    payload.update(
                        {
                            "status": "error",
                            "error_type": type(error).__name__,
                            "error_message": str(error),
                        }
                    )
                else:
                    payload.update(
                        {
                            "status": "completed",
                            "attempt_state": result.attempt.state.value,
                            "historical_planner_session_id": result.attempt.planner_session_id,
                            "historical_planner_turn_id": result.attempt.planner_turn_id,
                            "successor_dag_id": result.successor_dag.dag_id,
                            "proposal_fingerprint": result.proposal.proposal_fingerprint,
                        }
                    )
        except Exception as error:
            payload.update(
                {
                    "status": "error",
                    "error_type": type(error).__name__,
                    "error_message": str(error),
                }
            )
        finally:
            if replan is not None:
                await replan.close()
            if parent_binding is not None:
                await parent_binding.close()
            if application is not None:
                await application.close()
        payload["provider_call_count"] = len(_read_durable_json_lines(call_log))
        _write_durable_json(result_file, payload)
        controller_finished_event.set()

    asyncio.run(run())


@pytest.mark.asyncio
async def test_spawned_replan_controllers_have_one_winner_and_one_provider_call() -> None:
    context = mp.get_context("spawn")
    with tempfile.TemporaryDirectory() as directory:
        store, parent_id, _planner_id, _source = await _store_and_failed_source(directory)
        planner_ids = [
            await store.create_session(directory, "fixture", "fixture-model") for _ in range(2)
        ]
        barrier = context.Barrier(2)
        release = context.Event()
        queue = context.Queue()
        call_log = Path(directory) / "provider-calls"
        processes = [
            context.Process(
                target=_spawn_replan_race,
                args=(
                    str(store.database_path),
                    parent_id,
                    planner_id,
                    "spawn-race",
                    barrier,
                    release,
                    str(call_log),
                    queue,
                ),
            )
            for planner_id in planner_ids
        ]
        for process in processes:
            process.start()
        await asyncio.sleep(0.5)
        release.set()
        outcomes = [await asyncio.to_thread(queue.get, True, 30) for _ in processes]
        for process in processes:
            await asyncio.to_thread(process.join, 30)
            assert process.exitcode == 0
            process.close()
        assert all(outcome[0] in {"completed", "error"} for outcome in outcomes)
        assert sum(outcome[2] == 1 for outcome in outcomes) == 1
        assert sum(outcome[2] == 0 for outcome in outcomes) == 1
        assert call_log.read_text(encoding="utf-8").splitlines() == ["call"]
        attempt = await store.get_task_dag_replan_attempt("spawn-race")
        assert attempt is not None
        assert attempt.state is DagReplanAttemptState.COMPLETED


def test_schema_26_is_current_and_replan_tables_are_foreign_key_restricted() -> None:
    assert SCHEMA_VERSION == 26


@pytest.mark.asyncio
async def test_schema_25_to_26_migration_preserves_populated_dag_and_session() -> None:
    with tempfile.TemporaryDirectory() as directory:
        database = Path(directory) / "sessions.db"
        store = SqliteSessionStore(database)
        await store.initialize()
        session_id = await store.create_session(directory, "fixture", "fixture-model")
        dag = TaskDag.create(
            dag_id="pre-replan-migration",
            parent_session_id=session_id,
            nodes=(TaskDagNode("a", 0, "preserved"),),
            created_at=datetime.now(UTC),
        )
        await store.insert_task_dag(dag)
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("UPDATE schema_meta SET version = 25 WHERE singleton = 1")
        await store.initialize()
        with closing(sqlite3.connect(database)) as connection:
            assert connection.execute(
                "SELECT version FROM schema_meta WHERE singleton = 1"
            ).fetchone() == (26,)
            assert connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name IN ('orchestration_dag_replan_attempts', "
                "'orchestration_dag_replan_proposals') ORDER BY name"
            ).fetchall() == [
                ("orchestration_dag_replan_attempts",),
                ("orchestration_dag_replan_proposals",),
            ]
        assert await store.get_session(session_id) is not None
        assert await store.get_task_dag(dag.dag_id) == dag


@pytest.mark.parametrize(
    ("stage", "exit_code", "expected_state"),
    [
        ("model-committed", 73, DagReplanAttemptState.MODEL_COMMITTED),
        ("proposal-published", 74, DagReplanAttemptState.PROPOSAL_PUBLISHED),
        ("successor-dag-inserted", 75, DagReplanAttemptState.PROPOSAL_PUBLISHED),
        ("provider-turn-evidence", 76, DagReplanAttemptState.PROVIDER_FENCED),
    ],
)
@pytest.mark.asyncio
async def test_real_composition_replan_crash_matrix_reuses_durable_state_without_replay(
    stage: str,
    exit_code: int,
    expected_state: DagReplanAttemptState,
) -> None:
    context = mp.get_context("spawn")
    with tempfile.TemporaryDirectory(prefix=f"neuro-replan-crash-{stage}-") as directory:
        root = Path(directory)
        repository = _make_repository(root)
        state_dir = root / "state"
        state_dir.mkdir()
        (state_dir / "config.toml").write_text(
            """
[web_search]
mode = "disabled"

[web_fetch]
mode = "disabled"

[routing]
default = "fixture"

[providers.fixture]
protocol = "openai-chat"
model = "fixture-model"
base_url = "https://provider.invalid/v1"
api_key_env = "FIXTURE_KEY"
context_window_tokens = 131072
""",
            encoding="utf-8",
        )
        database = state_dir / "sessions.db"
        seed = SqliteSessionStore(database)
        await seed.initialize()
        parent_session_id = await seed.create_session(
            str(repository),
            "fixture",
            "fixture-model",
            sandbox_profile=SandboxProfile.OFF,
        )
        source_before = await _seed_failed_production_source(seed, parent_session_id)
        revision_id = f"real-crash-{stage}"
        marker = root / "replan-a.json"
        result_file = root / "replan-b.json"
        call_log = root / "replan-calls.jsonl"

        process_a = context.Process(
            target=_spawn_real_replan_crash,
            args=(
                str(root),
                str(repository),
                str(state_dir),
                parent_session_id,
                revision_id,
                str(marker),
                str(call_log),
                stage,
            ),
        )
        process_a.start()
        await asyncio.to_thread(process_a.join, 120)
        assert process_a.exitcode == exit_code
        process_a.close()
        snapshot = cast(
            dict[str, object],
            json.loads(marker.read_text(encoding="utf-8")),
        )
        assert snapshot["state"] == expected_state.value
        assert snapshot["parent_session_id"] == parent_session_id
        assert snapshot["source_dag_id"] == source_before.dag_id
        assert snapshot["source_generation"] == source_before.generation
        assert snapshot["planner_session_id"]
        assert snapshot["planner_turn_id"]
        assert snapshot["provider_call_count"] == 1
        assert len(_read_durable_json_lines(call_log)) == 1
        if stage == "provider-turn-evidence":
            assert snapshot["model_response"] is None
            assert snapshot["turn_id"] == snapshot["planner_turn_id"]
            assert snapshot["turn_last_stage"] == "model_output_started"
            assert snapshot["turn_request_started_count"] == 1
            assert snapshot["turn_output_started"] is True
        else:
            assert snapshot["model_response"] == _read_durable_json_lines(call_log)[0]["response"]

        process_b = context.Process(
            target=_spawn_real_replan_recovery,
            args=(
                str(root),
                str(repository),
                str(state_dir),
                parent_session_id,
                revision_id,
                str(marker),
                str(call_log),
                str(result_file),
            ),
        )
        process_b.start()
        await asyncio.to_thread(process_b.join, 120)
        assert process_b.exitcode == 0
        process_b.close()
        recovered = cast(
            dict[str, object],
            json.loads(result_file.read_text(encoding="utf-8")),
        )
        assert recovered["planner_session_id"] != snapshot["planner_session_id"]
        assert recovered["provider_call_count_before"] == 1
        assert recovered["provider_call_count_after"] == 1
        assert len(_read_durable_json_lines(call_log)) == 1

        observer = SqliteSessionStore(database)
        await observer.initialize()
        source_after = await observer.get_task_dag(source_before.dag_id)
        assert source_after == source_before
        attempt = await observer.get_task_dag_replan_attempt(revision_id)
        assert attempt is not None
        if stage == "provider-turn-evidence":
            assert recovered["status"] == "error"
            assert recovered["error_type"] == "ConfigurationError"
            assert "observable provider-turn evidence" in str(recovered["error_message"])
            assert recovered["durable_attempt_state"] == DagReplanAttemptState.INDETERMINATE.value
            assert attempt.state is DagReplanAttemptState.INDETERMINATE
            assert await observer.get_task_dag_replan_proposal(revision_id) is None
        else:
            assert recovered["status"] == "completed"
            assert recovered["attempt_state"] == DagReplanAttemptState.COMPLETED.value
            assert recovered["durable_attempt_state"] == DagReplanAttemptState.COMPLETED.value
            assert recovered["historical_planner_session_id"] == snapshot["planner_session_id"]
            assert recovered["historical_planner_turn_id"] == snapshot["planner_turn_id"]
            assert recovered["intended_successor_dag_id"] == snapshot["intended_successor_dag_id"]
            assert recovered["successor_dag_id"] == (
                snapshot["successor_dag_id"] or snapshot["intended_successor_dag_id"]
            )
            assert attempt.state is DagReplanAttemptState.COMPLETED
            proposal = await observer.get_task_dag_replan_proposal(revision_id)
            assert proposal is not None
            successor = await observer.get_task_dag(str(recovered["successor_dag_id"]))
            assert successor is not None
            assert successor.parent_session_id == parent_session_id
            assert successor.dag_id != source_before.dag_id


@pytest.mark.asyncio
async def test_real_composition_replan_controllers_race_with_one_provider_publication() -> None:
    context = mp.get_context("spawn")
    with tempfile.TemporaryDirectory(prefix="neuro-replan-controller-race-") as directory:
        root = Path(directory)
        repository = _make_repository(root)
        state_dir = root / "state"
        _write_fixture_config(state_dir)
        database = state_dir / "sessions.db"
        seed = SqliteSessionStore(database)
        await seed.initialize()
        parent_session_id = await seed.create_session(
            str(repository),
            "fixture",
            "fixture-model",
            sandbox_profile=SandboxProfile.OFF,
        )
        source_before = await _seed_failed_production_source(seed, parent_session_id)
        revision_id = "real-controller-race"
        call_log = root / "replan-calls.jsonl"
        start_barrier = context.Barrier(3)
        provider_started_event = context.Event()
        provider_release_event = context.Event()
        controller_finished_event = context.Event()
        processes = [
            context.Process(
                target=_spawn_real_replan_controller,
                args=(
                    str(root),
                    str(repository),
                    str(state_dir),
                    parent_session_id,
                    revision_id,
                    str(call_log),
                    start_barrier,
                    provider_started_event,
                    provider_release_event,
                    controller_finished_event,
                    str(root / f"controller-{index}.json"),
                ),
            )
            for index in (1, 2)
        ]
        for process in processes:
            process.start()
        outcomes: list[dict[str, object]] = []
        try:
            await asyncio.to_thread(start_barrier.wait, 120)
            assert await asyncio.to_thread(provider_started_event.wait, 120)
            assert await asyncio.to_thread(controller_finished_event.wait, 120)
            provider_release_event.set()
            for process in processes:
                await asyncio.to_thread(process.join, 120)
                assert process.exitcode == 0
            outcomes = [
                cast(
                    dict[str, object],
                    json.loads((root / f"controller-{index}.json").read_text(encoding="utf-8")),
                )
                for index in (1, 2)
            ]
        finally:
            provider_release_event.set()
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    await asyncio.to_thread(process.join, 15)
                process.close()

        assert sum(outcome["status"] == "completed" for outcome in outcomes) == 1
        assert sum(outcome["status"] == "error" for outcome in outcomes) == 1
        winner = next(outcome for outcome in outcomes if outcome["status"] == "completed")
        loser = next(outcome for outcome in outcomes if outcome["status"] == "error")
        assert winner["attempt_state"] == DagReplanAttemptState.COMPLETED.value
        assert loser["error_type"] == "ConfigurationError"
        assert winner["planner_session_id"] != loser["planner_session_id"]
        assert len(_read_durable_json_lines(call_log)) == 1
        assert all(cast(int, outcome["provider_call_count"]) <= 1 for outcome in outcomes)
        observer = SqliteSessionStore(database)
        await observer.initialize()
        attempt = await observer.get_task_dag_replan_attempt(revision_id)
        assert attempt is not None
        assert attempt.state is DagReplanAttemptState.COMPLETED
        assert await observer.get_task_dag(source_before.dag_id) == source_before
        with closing(sqlite3.connect(database)) as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM orchestration_dag_replan_proposals"
            ).fetchone() == (1,)
        successor = await observer.get_task_dag(attempt.successor_dag_id or "")
        assert successor is not None


@pytest.mark.asyncio
async def test_real_model_generated_failure_replan_successor_leader_writable_does_not_rerun_source_workers() -> (
    None
):
    with tempfile.TemporaryDirectory(prefix="neuro-replan-production-e2e-") as directory:
        root = Path(directory)
        repository = _make_repository(root)
        dirty_file = repository / "dirty-parent.txt"
        dirty_file.write_text("parent remains dirty\n", encoding="utf-8")
        before_status = _run_git(repository, "status", "--porcelain=v1")
        before_head = _run_git(repository, "rev-parse", "HEAD")
        state_dir = root / "state"
        _write_fixture_config(state_dir)
        state = _ProductionPlanningState(fail_source_worker_ids=frozenset({"b"}))

        def provider_factory(_config: Any, _failover: bool) -> ModelProvider:
            return cast(ModelProvider, _ReplanProductionProvider(state))

        application = None
        planner = None
        replan = None
        leader = None
        parent_binding = None
        environment = _production_planning_environment(root, state_dir)
        with patch.dict("os.environ", environment, clear=False):
            application = await ApplicationComposition.open(
                _production_planning_settings(repository),
                provider_factory=provider_factory,
            )
            store = cast(SqliteSessionStore, application.store)
            try:
                parent_session_id = await store.create_session(
                    str(repository),
                    "fixture",
                    "fixture-model",
                    sandbox_profile=SandboxProfile.OFF,
                )
                parent_binding = await application.create_binding(
                    resume_id=parent_session_id,
                    capabilities=_parent_capability(repository),
                )
                planner = await application.create_model_planning_service(
                    parent_binding=parent_binding,
                )
                planned = await planner.run(
                    RunModelDagPlanningRequest("replan-production-plan", "decompose objective")
                )
                await planner.close()
                planner = None
                leader = await application.create_leader_service(parent_binding=parent_binding)
                source_run = asyncio.create_task(
                    leader.run(RunLeaderRequest(planned.dag.dag_id, "execute source DAG"))
                )
                await asyncio.wait_for(state.fanout_started.wait(), timeout=120)
                state.release_fanout.set()
                source_completed = await asyncio.wait_for(source_run, timeout=180)
                assert source_completed.dag.state is TaskDagState.FAILED
                assert source_completed.final_response == "source DAG failed safely"
                source_before = await store.get_task_dag(planned.dag.dag_id)
                assert source_before is not None
                assert source_before.state is TaskDagState.FAILED
                assert source_before.node("a").state is TaskDagNodeState.COMPLETED
                assert source_before.node("b").state is TaskDagNodeState.FAILED
                assert source_before.node("c").state is TaskDagNodeState.COMPLETED
                assert source_before.node("d").state is TaskDagNodeState.SKIPPED
                source_calls = [
                    node_id for node_id, phase in state.worker_call_phases if phase == "source"
                ]
                assert source_calls[0] == "a"
                assert set(source_calls[1:]) == {"b", "c"}
                source_worker_count = len(state.worker_calls)
                state.fanout_started = asyncio.Event()
                state.release_fanout = asyncio.Event()
                state.started = []
                state.leader_calls = 0
                state.max_active = 0

                replan = await application.create_task_dag_replan_service(
                    parent_binding=parent_binding,
                )
                revised = await replan.run(
                    RunTaskDagReplanRequest("replan-production-revision", source_before.dag_id)
                )
                assert revised.successor_dag.dag_id != source_before.dag_id
                assert [node.node_id for node in revised.successor_dag.nodes] == [
                    "repair-b",
                    "repair-c",
                    "repair-d",
                ]
                assert revised.successor_dag.max_parallel == 2
                await replan.close()
                replan = None

                running = asyncio.create_task(
                    leader.run(RunLeaderRequest(revised.successor_dag.dag_id, "execute recovery"))
                )
                await asyncio.wait_for(state.fanout_started.wait(), timeout=120)
                during = await store.get_task_dag(revised.successor_dag.dag_id)
                assert during is not None
                assert set(during.running_node_ids) == {"repair-b", "repair-c"}
                assert state.max_active == 2
                state.release_fanout.set()
                completed = await asyncio.wait_for(running, timeout=180)
                assert completed.dag.state is TaskDagState.COMPLETED
                assert completed.final_response == "replan DAG completed"

                assert state.planner_calls == 2
                assert state.zero_tool_calls == 8
                assert len(state.worker_calls) == source_worker_count + 3
                source_calls = [
                    node_id for node_id, phase in state.worker_call_phases if phase == "source"
                ]
                assert source_calls[0] == "a"
                assert set(source_calls[1:]) == {"b", "c"}
                assert {
                    node_id for node_id, phase in state.worker_call_phases if phase == "replan"
                } == {"b", "c", "d"}
                assert state.timeline.index("complete:b") < state.timeline.index("start:d")
                assert state.timeline.index("complete:c") < state.timeline.index("start:d")
                source_after = await store.get_task_dag(source_before.dag_id)
                assert source_after == source_before
                assert _run_git(repository, "status", "--porcelain=v1") == before_status
                assert _run_git(repository, "rev-parse", "HEAD") == before_head
                assert dirty_file.read_text(encoding="utf-8") == "parent remains dirty\n"
            finally:
                state.release_fanout.set()
                if leader is not None:
                    await leader.close()
                if replan is not None:
                    await replan.close()
                if planner is not None:
                    await planner.close()
                if parent_binding is not None:
                    await parent_binding.close()
                await application.close()
