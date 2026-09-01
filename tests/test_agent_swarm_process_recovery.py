from __future__ import annotations

import asyncio
import json
import multiprocessing as mp
import os
import sqlite3
import subprocess
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NoReturn, cast
from unittest.mock import patch

import pytest

from neuro_code.application.permissions.policy import PermissionMode
from neuro_code.application.ports.model import ModelCapabilitySet, ModelProvider, ModelToolPolicy
from neuro_code.application.sessions.binding import ConversationBinding
from neuro_code.application.settings import ApplicationSettings
from neuro_code.application.workflows.agent_swarm import RunAgentSwarmRequest
from neuro_code.application.workflows.leader import LeaderRunResult, RunLeaderRequest
from neuro_code.application.workflows.subagent_capabilities import SubagentCapabilitySet
from neuro_code.bootstrap.composition import ApplicationComposition
from neuro_code.domain.agent_swarm import (
    AgentSwarmRun,
    AgentSwarmRunState,
    objective_fingerprint,
)
from neuro_code.domain.conversation.events import ModelCompleted, ModelEvent
from neuro_code.domain.sandbox.models import SandboxProfile
from neuro_code.domain.task_dag import TaskDag, TaskDagNode, TaskDagNodeState, TaskDagState
from neuro_code.infrastructure.persistence.sqlite_session import SqliteSessionStore
from neuro_code.shared.errors import ConfigurationError

_RECOVERY_RESPONSE = json.dumps(
    {
        "nodes": [
            {
                "id": "swarm-recovery-node",
                "prompt": "swarm-recovery-node",
                "depends_on": [],
            }
        ],
        "max_parallel": 1,
        "reason": "bounded fresh-process recovery",
    },
    ensure_ascii=False,
)

_REPLAN_RESPONSE = json.dumps(
    {
        "nodes": [{"id": "repair-node", "prompt": "repair-node", "depends_on": []}],
        "max_parallel": 1,
        "reason": "bounded fresh-process replan recovery",
    },
    ensure_ascii=False,
)

_RESOURCE_TABLES = (
    "session_tasks",
    "subagent_links",
    "writable_subagent_leases",
    "parent_context_relays",
    "task_dag_dependency_relays",
    "task_dags",
    "leader_attempts",
    "leader_decisions",
    "orchestration_planning_attempts",
    "orchestration_plan_proposals",
    "orchestration_dag_replan_attempts",
    "orchestration_dag_replan_proposals",
    "orchestration_swarm_runs",
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


def _provider_counts(path: Path) -> dict[str, int]:
    counts = Counter(str(record["kind"]) for record in _read_durable_json_lines(path))
    return dict(sorted(counts.items()))


def _run_git(directory: Path, *arguments: str) -> None:
    result = subprocess.run(
        ["git", *arguments],
        cwd=directory,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr.decode(errors="replace"))


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
    state_dir.mkdir(parents=True)
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


def _settings(repository: Path) -> ApplicationSettings:
    return ApplicationSettings(
        cwd=repository,
        sandbox="off",
        permission_mode=PermissionMode.BYPASS,
        max_steps=8,
    )


def _environment(root: Path, state_dir: Path) -> dict[str, str]:
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


class _FreshSwarmProvider:
    provider_name = "fixture"
    model_name = "fixture-model"
    context_affinity = "fixture-swarm-recovery"
    capabilities = ModelCapabilitySet.all_unknown()

    def __init__(self, call_log: Path) -> None:
        self._call_log = call_log
        self._leader_calls = 0

    def _record(self, kind: str, *, node_id: str | None = None) -> None:
        payload: dict[str, object] = {"kind": kind, "pid": os.getpid()}
        if node_id is not None:
            payload["node_id"] = node_id
        _append_durable_json_line(self._call_log, payload)

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
            self._record("replan")
            yield ModelCompleted("stop", response_text=_REPLAN_RESPONSE)
            return
        if "bounded DAG Planner" in contents:
            if tools:
                raise AssertionError("Planner received tools")
            self._record("planner")
            yield ModelCompleted("stop", response_text=_RECOVERY_RESPONSE)
            return
        if "Leader decision authority" in contents:
            if tools:
                raise AssertionError("Leader received tools")
            self._leader_calls += 1
            node_id = next(
                (
                    candidate
                    for candidate in ("repair-node", "swarm-recovery-node")
                    if candidate in contents
                ),
                None,
            )
            action = (
                f'{{"action":"SELECT_NODE","node_id":"{node_id}"}}'
                if node_id is not None and self._leader_calls == 1
                else '{"action":"FINALIZE","summary":"swarm recovery completed"}'
            )
            self._record("leader")
            yield ModelCompleted("stop", response_text=action)
            return
        node_id = next(
            (
                candidate
                for candidate in ("repair-node", "swarm-recovery-node")
                if candidate in contents
            ),
            None,
        )
        if node_id is None:
            raise AssertionError("worker fixture could not identify its node")
        if not tools:
            raise AssertionError("Writable worker unexpectedly received no tools")
        self._record("worker", node_id=node_id)
        yield ModelCompleted("stop", response_text=f"result {node_id}")


def _provider_factory(call_log: Path) -> Any:
    def factory(_config: Any, _failover: bool) -> ModelProvider:
        return cast(ModelProvider, _FreshSwarmProvider(call_log))

    return factory


def _resource_counts(state_dir: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    database = state_dir / "sessions.db"
    with closing(sqlite3.connect(database)) as connection:
        available = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        for table in _RESOURCE_TABLES:
            if table in available:
                row = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
                counts[table] = int(row[0]) if row is not None else 0
    for filename, table in (
        ("worktrees.db", "managed_worktrees"),
        ("checkpoints.db", "checkpoints"),
    ):
        database = state_dir / filename
        if not database.exists():
            continue
        with closing(sqlite3.connect(database)) as connection:
            row = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            counts[table] = int(row[0]) if row is not None else 0
    return counts


async def _record_swarm_snapshot(
    application: ApplicationComposition,
    *,
    marker_path: Path,
    run_id: str,
    call_log: Path,
    stage: str,
) -> None:
    store = cast(SqliteSessionStore, application.store)
    run = await store.get_swarm_run(run_id)
    assert run is not None
    planning_attempt = await store.get_model_planning_attempt(run.planning_id)
    planning_proposal = await store.get_model_planning_proposal(run.planning_id)
    planning_dag = (
        await store.get_task_dag(planning_attempt.intended_dag_id)
        if planning_attempt is not None
        else None
    )
    replan_attempt = (
        await store.get_task_dag_replan_attempt(run.replan_revision_id)
        if run.replan_revision_id is not None
        else None
    )
    replan_proposal = (
        await store.get_task_dag_replan_proposal(run.replan_revision_id)
        if run.replan_revision_id is not None
        else None
    )
    current_dag = (
        await store.get_task_dag(run.current_dag_id) if run.current_dag_id is not None else None
    )
    decisions = (
        await store.list_leader_decisions(run.current_dag_id)
        if run.current_dag_id is not None
        else ()
    )
    successor = (
        await store.get_task_dag(replan_attempt.successor_dag_id)
        if replan_attempt is not None and replan_attempt.successor_dag_id is not None
        else None
    )
    _write_durable_json(
        marker_path,
        {
            "stage": stage,
            "run_state": run.state.value,
            "run_generation": run.generation,
            "planning_id": run.planning_id,
            "planner_session_id": run.planner_session_id,
            "planner_turn_id": run.planner_turn_id,
            "proposal_fingerprint": run.proposal_fingerprint,
            "root_dag_id": run.root_dag_id,
            "current_dag_id": run.current_dag_id,
            "current_dag_generation": run.current_dag_generation,
            "current_dag_definition_fingerprint": run.current_dag_definition_fingerprint,
            "replan_revision_id": run.replan_revision_id,
            "successor_dag_id": run.successor_dag_id,
            "final_response": run.final_response,
            "final_result_fingerprint": run.final_result_fingerprint,
            "planning_state": planning_attempt.state.value if planning_attempt else None,
            "planning_planner_session_id": (
                planning_attempt.planner_session_id if planning_attempt else None
            ),
            "planning_planner_turn_id": (
                planning_attempt.planner_turn_id if planning_attempt else None
            ),
            "planning_proposal_fingerprint": (
                planning_proposal.proposal_fingerprint if planning_proposal else None
            ),
            "planning_dag_id": planning_dag.dag_id if planning_dag else None,
            "planning_dag_state": planning_dag.state.value if planning_dag else None,
            "planning_dag_definition_fingerprint": (
                planning_dag.definition_fingerprint if planning_dag else None
            ),
            "current_dag_state": current_dag.state.value if current_dag else None,
            "current_dag_definition_fingerprint_observed": (
                current_dag.definition_fingerprint if current_dag else None
            ),
            "leader_decision_count": len(decisions),
            "replan_state": replan_attempt.state.value if replan_attempt else None,
            "replan_successor_dag_id": (
                replan_attempt.successor_dag_id if replan_attempt else None
            ),
            "replan_successor_state": successor.state.value if successor else None,
            "replan_proposal_count": (1 if replan_proposal is not None else 0),
            "provider_counts": _provider_counts(call_log),
            "resources": _resource_counts(application.config.state_dir),
        },
    )


def _new_dag(parent_session_id: str, dag_id: str, node_id: str) -> TaskDag:
    return TaskDag.create(
        dag_id=dag_id,
        parent_session_id=parent_session_id,
        nodes=(TaskDagNode(node_id, 0, node_id),),
        created_at=datetime.now(UTC),
        max_parallel=1,
    )


async def _seed_failed_dag(
    store: SqliteSessionStore,
    parent_session_id: str,
    dag_id: str,
) -> TaskDag:
    dag = _new_dag(parent_session_id, dag_id, "source-node")
    await store.insert_task_dag(dag)
    now = datetime.now(UTC)
    running_node = replace(
        dag.node("source-node"),
        state=TaskDagNodeState.RUNNING,
        generation=1,
        parent_task_id="source-task",
        execution_owner_pid=os.getpid(),
        execution_owner_token="source-owner",
    )
    running = await store.claim_task_dag_node(
        dag_id,
        running_node,
        expected_generation=0,
        expected_state=TaskDagNodeState.READY,
        updated_at=now,
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
        dag_id,
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
    return await store.compare_and_transition_task_dag(
        failed,
        expected_generation=finished.generation,
        expected_state=TaskDagState.RUNNING,
    )


async def _seed_executing_swarm(
    store: SqliteSessionStore,
    parent_session_id: str,
    *,
    run_id: str,
    objective: str,
    dag: TaskDag,
    replan_revision_id: str | None = None,
) -> AgentSwarmRun:
    now = datetime.now(UTC)
    run = AgentSwarmRun(
        swarm_run_id=run_id,
        parent_session_id=parent_session_id,
        objective_fingerprint=objective_fingerprint(objective),
        planning_id=f"swarm-planning-{run_id}",
        state=AgentSwarmRunState.EXECUTING,
        generation=0,
        owner_id="seed-owner",
        owner_pid=2_147_483_647,
        owner_token="seed-owner-token",
        lease_expires_at=now + timedelta(minutes=5),
        created_at=now,
        updated_at=now,
        root_dag_id=dag.dag_id,
        current_dag_id=dag.dag_id,
        current_dag_generation=dag.generation,
        current_dag_definition_fingerprint=dag.definition_fingerprint,
        replan_revision_id=replan_revision_id,
    )
    claim = await store.claim_swarm_run(
        run,
        now=now,
        owner_is_alive=lambda _pid: False,
    )
    assert claim.acquired is True
    return claim.run


def _spawn_swarm_crash(
    root_directory: str,
    repository_directory: str,
    state_directory: str,
    parent_session_id: str,
    run_id: str,
    objective: str,
    marker: str,
    provider_call_log: str,
    stage: str,
) -> None:
    async def run() -> NoReturn:
        root = Path(root_directory)
        repository = Path(repository_directory)
        state_dir = Path(state_directory)
        marker_path = Path(marker)
        call_log = Path(provider_call_log)
        application: ApplicationComposition | None = None
        parent_binding: ConversationBinding | None = None
        swarm = None
        with patch.dict("os.environ", _environment(root, state_dir), clear=False):
            try:
                application = await ApplicationComposition.open(
                    _settings(repository),
                    provider_factory=_provider_factory(call_log),
                )
                store = cast(SqliteSessionStore, application.store)
                parent_binding = await application.create_binding(
                    resume_id=parent_session_id,
                    capabilities=_parent_capability(repository),
                )
                swarm = await application.create_agent_swarm_service(
                    parent_binding=parent_binding,
                )
                original = store.compare_and_transition_swarm_run

                async def hooked(
                    proposed: AgentSwarmRun,
                    *,
                    expected_generation: int,
                    expected_state: AgentSwarmRunState,
                ) -> AgentSwarmRun:
                    if (
                        stage == "planner-planned"
                        and expected_state is AgentSwarmRunState.PLANNING
                        and proposed.state is AgentSwarmRunState.PLANNED
                    ):
                        await _record_swarm_snapshot(
                            application,
                            marker_path=marker_path,
                            run_id=run_id,
                            call_log=call_log,
                            stage=stage,
                        )
                        os._exit(81)
                    if (
                        stage == "terminal-dag-finalizing"
                        and expected_state is AgentSwarmRunState.EXECUTING
                        and proposed.state is AgentSwarmRunState.FINALIZING
                    ):
                        await _record_swarm_snapshot(
                            application,
                            marker_path=marker_path,
                            run_id=run_id,
                            call_log=call_log,
                            stage=stage,
                        )
                        os._exit(82)
                    if (
                        stage == "replan-successor-switch"
                        and expected_state is AgentSwarmRunState.REPLANNING
                        and proposed.state is AgentSwarmRunState.EXECUTING
                        and proposed.successor_dag_id is not None
                    ):
                        await _record_swarm_snapshot(
                            application,
                            marker_path=marker_path,
                            run_id=run_id,
                            call_log=call_log,
                            stage=stage,
                        )
                        os._exit(83)
                    result = await original(
                        proposed,
                        expected_generation=expected_generation,
                        expected_state=expected_state,
                    )
                    if (
                        stage == "finalizing-completed"
                        and expected_state is AgentSwarmRunState.EXECUTING
                        and proposed.state is AgentSwarmRunState.FINALIZING
                    ):
                        await _record_swarm_snapshot(
                            application,
                            marker_path=marker_path,
                            run_id=run_id,
                            call_log=call_log,
                            stage=stage,
                        )
                        os._exit(84)
                    return result

                with patch.object(
                    store,
                    "compare_and_transition_swarm_run",
                    new=hooked,
                ):
                    await swarm.run(RunAgentSwarmRequest(run_id, objective))
                raise AssertionError(f"fresh Swarm process did not crash at {stage}")
            finally:
                if swarm is not None:
                    await swarm.close()
                if parent_binding is not None:
                    await parent_binding.close()
                if application is not None:
                    await application.close()

    asyncio.run(run())


def _spawn_swarm_resume(
    root_directory: str,
    repository_directory: str,
    state_directory: str,
    parent_session_id: str,
    run_id: str,
    objective: str,
    provider_call_log: str,
    result_path: str,
) -> None:
    async def run() -> None:
        root = Path(root_directory)
        repository = Path(repository_directory)
        state_dir = Path(state_directory)
        call_log = Path(provider_call_log)
        result_file = Path(result_path)
        before = _provider_counts(call_log)
        payload: dict[str, object] = {
            "status": "error",
            "provider_counts_before": before,
        }
        application: ApplicationComposition | None = None
        parent_binding: ConversationBinding | None = None
        swarm = None
        with patch.dict("os.environ", _environment(root, state_dir), clear=False):
            try:
                application = await ApplicationComposition.open(
                    _settings(repository),
                    provider_factory=_provider_factory(call_log),
                )
                parent_binding = await application.create_binding(
                    resume_id=parent_session_id,
                    capabilities=_parent_capability(repository),
                )
                swarm = await application.create_agent_swarm_service(
                    parent_binding=parent_binding,
                )
                try:
                    result = await swarm.run(RunAgentSwarmRequest(run_id, objective))
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
                            "run_state": result.run.state.value,
                            "planning_id": result.run.planning_id,
                            "planner_session_id": result.run.planner_session_id,
                            "planner_turn_id": result.run.planner_turn_id,
                            "proposal_fingerprint": result.run.proposal_fingerprint,
                            "root_dag_id": result.run.root_dag_id,
                            "current_dag_id": result.run.current_dag_id,
                            "current_dag_generation": result.run.current_dag_generation,
                            "current_dag_definition_fingerprint": (
                                result.run.current_dag_definition_fingerprint
                            ),
                            "replan_revision_id": result.run.replan_revision_id,
                            "successor_dag_id": result.run.successor_dag_id,
                            "final_response": result.final_response,
                            "final_result_fingerprint": result.run.final_result_fingerprint,
                            "dag_state": result.dag.state.value,
                            "dag_definition_fingerprint": result.dag.definition_fingerprint,
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
                if swarm is not None:
                    await swarm.close()
                if parent_binding is not None:
                    await parent_binding.close()
                if application is not None:
                    await application.close()
        payload["provider_counts_after"] = _provider_counts(call_log)
        observer = SqliteSessionStore(state_dir / "sessions.db")
        await observer.initialize()
        persisted = await observer.get_swarm_run(run_id)
        payload["durable_state"] = persisted.state.value if persisted is not None else None
        payload["durable_run_generation"] = persisted.generation if persisted is not None else None
        root_dag = (
            await observer.get_task_dag(persisted.root_dag_id)
            if persisted is not None and persisted.root_dag_id is not None
            else None
        )
        payload["root_dag_id"] = root_dag.dag_id if root_dag is not None else None
        payload["root_dag_state"] = root_dag.state.value if root_dag is not None else None
        payload["root_dag_generation"] = root_dag.generation if root_dag is not None else None
        payload["root_dag_definition_fingerprint"] = (
            root_dag.definition_fingerprint if root_dag is not None else None
        )
        payload["resources"] = _resource_counts(state_dir)
        _write_durable_json(result_file, payload)

    asyncio.run(run())


async def _join_process(process: Any, expected_exit_code: int) -> None:
    await asyncio.to_thread(process.join, 240)
    if process.is_alive():
        process.terminate()
        await asyncio.to_thread(process.join, 15)
    try:
        assert process.exitcode == expected_exit_code
    finally:
        process.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stage", "exit_code"),
    [
        ("planner-planned", 81),
        ("terminal-dag-finalizing", 82),
        ("replan-successor-switch", 83),
        ("finalizing-completed", 84),
    ],
)
async def test_real_composition_swarm_fresh_process_recovery_matrix(
    tmp_path: Path,
    stage: str,
    exit_code: int,
) -> None:
    context = mp.get_context("spawn")
    root = tmp_path
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
    objective = "bounded fresh-process Swarm recovery objective"
    run_id = f"fresh-process-{stage}"
    root_dag: TaskDag | None = None
    if stage == "replan-successor-switch":
        root_dag = await _seed_failed_dag(seed, parent_session_id, "swarm-source-dag")
        await _seed_executing_swarm(
            seed,
            parent_session_id,
            run_id=run_id,
            objective=objective,
            dag=root_dag,
        )
    elif stage != "planner-planned":
        root_dag = _new_dag(parent_session_id, f"{run_id}-dag", "swarm-recovery-node")
        await seed.insert_task_dag(root_dag)
        await _seed_executing_swarm(
            seed,
            parent_session_id,
            run_id=run_id,
            objective=objective,
            dag=root_dag,
        )

    marker = root / "swarm-a.json"
    result_file = root / "swarm-b.json"
    call_log = root / "swarm-provider-calls.jsonl"
    process_a = context.Process(
        target=_spawn_swarm_crash,
        args=(
            str(root),
            str(repository),
            str(state_dir),
            parent_session_id,
            run_id,
            objective,
            str(marker),
            str(call_log),
            stage,
        ),
    )
    process_a.start()
    await _join_process(process_a, exit_code)
    snapshot = cast(dict[str, object], json.loads(marker.read_text(encoding="utf-8")))
    assert snapshot["stage"] == stage

    process_b = context.Process(
        target=_spawn_swarm_resume,
        args=(
            str(root),
            str(repository),
            str(state_dir),
            parent_session_id,
            run_id,
            objective,
            str(call_log),
            str(result_file),
        ),
    )
    process_b.start()
    await _join_process(process_b, 0)
    recovered = cast(dict[str, object], json.loads(result_file.read_text(encoding="utf-8")))
    assert recovered["status"] == "completed"
    assert recovered["run_state"] == AgentSwarmRunState.COMPLETED.value
    assert recovered["durable_state"] == AgentSwarmRunState.COMPLETED.value
    assert recovered["final_response"] == "swarm recovery completed"
    assert recovered["final_result_fingerprint"]
    assert recovered["provider_counts_before"] == snapshot["provider_counts"]
    assert recovered["resources"]

    if stage == "planner-planned":
        assert snapshot["run_state"] == AgentSwarmRunState.PLANNING.value
        assert snapshot["planning_state"] == "completed"
        assert snapshot["planning_dag_state"] == TaskDagState.READY.value
        assert snapshot["planning_dag_id"]
        assert snapshot["planning_proposal_fingerprint"]
        assert snapshot["planning_planner_session_id"]
        assert snapshot["planning_planner_turn_id"]
        assert snapshot["provider_counts"] == {"planner": 1}
        assert recovered["planning_id"] == snapshot["planning_id"]
        assert recovered["planner_session_id"] == snapshot["planning_planner_session_id"]
        assert recovered["planner_turn_id"] == snapshot["planning_planner_turn_id"]
        assert recovered["proposal_fingerprint"] == snapshot["planning_proposal_fingerprint"]
        assert recovered["root_dag_id"] == snapshot["planning_dag_id"]
        assert recovered["current_dag_id"] == snapshot["planning_dag_id"]
        assert (
            recovered["current_dag_definition_fingerprint"]
            == (snapshot["planning_dag_definition_fingerprint"])
        )
        assert recovered["provider_counts_after"] == {
            "leader": 2,
            "planner": 1,
            "worker": 1,
        }
    elif stage == "terminal-dag-finalizing":
        assert snapshot["run_state"] == AgentSwarmRunState.EXECUTING.value
        assert snapshot["current_dag_state"] == TaskDagState.COMPLETED.value
        assert snapshot["leader_decision_count"] == 2
        assert snapshot["provider_counts"] == {"leader": 2, "worker": 1}
        assert recovered["current_dag_id"] == snapshot["current_dag_id"]
        assert (
            recovered["current_dag_definition_fingerprint"]
            == (snapshot["current_dag_definition_fingerprint"])
        )
        assert recovered["provider_counts_after"] == snapshot["provider_counts"]
        assert recovered["resources"] == snapshot["resources"]
    elif stage == "replan-successor-switch":
        assert snapshot["run_state"] == AgentSwarmRunState.REPLANNING.value
        assert snapshot["current_dag_state"] == TaskDagState.FAILED.value
        assert snapshot["replan_state"] == "completed"
        assert snapshot["replan_successor_state"] == TaskDagState.READY.value
        assert snapshot["successor_dag_id"] is None
        assert snapshot["replan_successor_dag_id"]
        assert snapshot["provider_counts"] == {"leader": 1, "replan": 1}
        assert recovered["root_dag_id"] == snapshot["root_dag_id"]
        assert (
            recovered["root_dag_definition_fingerprint"]
            == (snapshot["current_dag_definition_fingerprint_observed"])
        )
        assert recovered["root_dag_generation"] == snapshot["current_dag_generation"]
        assert recovered["root_dag_state"] == TaskDagState.FAILED.value
        assert recovered["current_dag_id"] == snapshot["replan_successor_dag_id"]
        assert recovered["successor_dag_id"] == snapshot["replan_successor_dag_id"]
        assert recovered["replan_revision_id"] == snapshot["replan_revision_id"]
        assert recovered["provider_counts_after"] == {
            "leader": 3,
            "replan": 1,
            "worker": 1,
        }
        assert [
            record.get("node_id")
            for record in _read_durable_json_lines(call_log)
            if record.get("kind") == "worker"
        ] == ["repair-node"]
    else:
        assert stage == "finalizing-completed"
        assert snapshot["run_state"] == AgentSwarmRunState.FINALIZING.value
        assert snapshot["final_response"] == "swarm recovery completed"
        assert snapshot["final_result_fingerprint"]
        assert snapshot["current_dag_state"] == TaskDagState.COMPLETED.value
        assert recovered["current_dag_id"] == snapshot["current_dag_id"]
        assert recovered["final_response"] == snapshot["final_response"]
        assert recovered["final_result_fingerprint"] == snapshot["final_result_fingerprint"]
        assert recovered["provider_counts_after"] == snapshot["provider_counts"]
        assert recovered["resources"] == snapshot["resources"]

    resources = cast(dict[str, int], recovered["resources"])
    assert resources["orchestration_swarm_runs"] == 1
    assert resources["orchestration_planning_attempts"] == (1 if stage == "planner-planned" else 0)
    assert resources["orchestration_plan_proposals"] == (1 if stage == "planner-planned" else 0)
    assert resources["orchestration_dag_replan_attempts"] == (
        1 if stage == "replan-successor-switch" else 0
    )
    assert resources["orchestration_dag_replan_proposals"] == (
        1 if stage == "replan-successor-switch" else 0
    )
    assert resources["task_dags"] == (2 if stage == "replan-successor-switch" else 1)
    assert resources["writable_subagent_leases"] == 1
    assert resources["leader_decisions"] == (3 if stage == "replan-successor-switch" else 2)


class _CompositionIndeterminateLeader:
    def __init__(self, dag: TaskDag) -> None:
        self._dag = dag
        self.calls = 0
        self.closed = 0

    async def run(self, request: RunLeaderRequest, *, sink: Any = None) -> LeaderRunResult:
        del sink
        assert request.dag_id == self._dag.dag_id
        self.calls += 1
        uncertain_node = replace(
            self._dag.node("swarm-recovery-node"),
            state=TaskDagNodeState.INDETERMINATE,
            generation=self._dag.node("swarm-recovery-node").generation + 1,
        )
        uncertain = replace(
            self._dag,
            nodes=(uncertain_node,),
            state=TaskDagState.INDETERMINATE,
            generation=self._dag.generation + 1,
            updated_at=datetime.now(UTC),
        )
        return LeaderRunResult(uncertain, None, ())

    async def close(self) -> None:
        self.closed += 1


@pytest.mark.asyncio
async def test_real_composition_indeterminate_lower_result_is_terminal_and_not_replanned(
    tmp_path: Path,
) -> None:
    root = tmp_path
    repository = _make_repository(root)
    state_dir = root / "state"
    _write_fixture_config(state_dir)
    with patch.dict("os.environ", _environment(root, state_dir), clear=False):
        application = await ApplicationComposition.open(
            _settings(repository),
            provider_factory=_provider_factory(root / "provider-calls.jsonl"),
        )
        parent_binding: ConversationBinding | None = None
        swarm = None
        try:
            parent_session_id = await application.store.create_session(
                str(repository),
                "fixture",
                "fixture-model",
                sandbox_profile=SandboxProfile.OFF,
            )
            dag = _new_dag(
                parent_session_id, "indeterminate-composition-dag", "swarm-recovery-node"
            )
            await cast(SqliteSessionStore, application.store).insert_task_dag(dag)
            await _seed_executing_swarm(
                cast(SqliteSessionStore, application.store),
                parent_session_id,
                run_id="indeterminate-composition-run",
                objective="bounded indeterminate composition objective",
                dag=dag,
            )
            parent_binding = await application.create_binding(
                resume_id=parent_session_id,
                capabilities=_parent_capability(repository),
            )
            leader = _CompositionIndeterminateLeader(dag)
            planner_factory_called = False
            replanner_factory_called = False

            async def forbidden_planner(
                *, parent_binding: ConversationBinding, timeout_seconds: float
            ) -> NoReturn:
                del parent_binding, timeout_seconds
                nonlocal planner_factory_called
                planner_factory_called = True
                raise AssertionError("indeterminate Swarm must not invoke Planner")

            async def forbidden_replanner(
                *, parent_binding: ConversationBinding, timeout_seconds: float
            ) -> NoReturn:
                del parent_binding, timeout_seconds
                nonlocal replanner_factory_called
                replanner_factory_called = True
                raise AssertionError("indeterminate Swarm must not invoke Replan")

            async def indeterminate_leader(
                *, parent_binding: ConversationBinding, timeout_seconds: float
            ) -> _CompositionIndeterminateLeader:
                del parent_binding, timeout_seconds
                return leader

            with (
                patch.object(application, "create_model_planning_service", new=forbidden_planner),
                patch.object(
                    application, "create_task_dag_replan_service", new=forbidden_replanner
                ),
                patch.object(application, "create_leader_service", new=indeterminate_leader),
            ):
                swarm = await application.create_agent_swarm_service(
                    parent_binding=parent_binding,
                )
                with pytest.raises(ConfigurationError, match="uncertain"):
                    await swarm.run(
                        RunAgentSwarmRequest(
                            "indeterminate-composition-run",
                            "bounded indeterminate composition objective",
                        )
                    )
            persisted = await cast(SqliteSessionStore, application.store).get_swarm_run(
                "indeterminate-composition-run"
            )
            assert persisted is not None
            assert persisted.state is AgentSwarmRunState.INDETERMINATE
            assert leader.calls == leader.closed == 1
            assert planner_factory_called is False
            assert replanner_factory_called is False
            assert _provider_counts(root / "provider-calls.jsonl") == {}
            resources = _resource_counts(state_dir)
            assert resources["orchestration_swarm_runs"] == 1
            assert resources["task_dags"] == 1
            assert resources["writable_subagent_leases"] == 0
            assert resources["orchestration_dag_replan_proposals"] == 0
        finally:
            if swarm is not None:
                await swarm.close()
            if parent_binding is not None:
                await parent_binding.close()
            await application.close()
