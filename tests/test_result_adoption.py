from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import sqlite3
import subprocess
from collections.abc import AsyncIterator, Sequence
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from neuro_code.application.permissions.contracts import PermissionApproval
from neuro_code.application.permissions.policy import (
    PermissionEffect,
    PermissionMode,
    PermissionRule,
)
from neuro_code.application.ports.model import ModelCapabilitySet, ModelProvider, ModelToolPolicy
from neuro_code.application.ports.result_adoption import (
    ParentWorkspaceSnapshot,
    ResultAdoptionError,
    ResultAdoptionTargetRecord,
    WorkspaceMutationRequest,
    WorkspaceMutationResult,
)
from neuro_code.application.ports.task_dag import TaskDagStore
from neuro_code.application.ports.tools import ToolContext
from neuro_code.application.ports.writable_subagent import WritableSubagentLeaseStore
from neuro_code.application.runtime.agent import AgentRunResult
from neuro_code.application.sessions.binding import ConversationBinding, ConversationRunner
from neuro_code.application.settings import ApplicationSettings
from neuro_code.application.workflows import result_adoption as result_adoption_workflow
from neuro_code.application.workflows.result_adoption import ResultAdoptionApplicationService
from neuro_code.application.workflows.subagent_capabilities import SubagentCapabilitySet
from neuro_code.application.workflows.task_dag import (
    CreateTaskDagRequest,
    RunTaskDagRequest,
    TaskDagApplicationService,
)
from neuro_code.application.workflows.writable_subagent import (
    RunWritableSubagentRequest,
    WritableSubagentApplicationService,
    WritableSubagentRuntimeFactory,
)
from neuro_code.bootstrap.composition import ApplicationComposition
from neuro_code.domain.agent_swarm import (
    AgentSwarmRun,
    AgentSwarmRunState,
    objective_fingerprint,
    terminal_result_fingerprint,
)
from neuro_code.domain.checkpoints import (
    CheckpointId,
    CheckpointState,
    WorkspaceCheckpoint,
    WorkspaceFileEntry,
    WorkspaceFileKind,
    WorkspaceFileScope,
    WorkspaceProjection,
    workspace_projection_fingerprint,
)
from neuro_code.domain.conversation.context import ModelContext
from neuro_code.domain.conversation.events import ModelEvent
from neuro_code.domain.result_adoption import (
    MAX_RESULT_ADOPTION_LEASE_SECONDS,
    ResultAdoptionOperation,
    ResultAdoptionPlan,
    ResultAdoptionRequest,
    ResultAdoptionState,
    ResultAdoptionTarget,
    ResultAdoptionTargetState,
    workspace_entry_fingerprint,
)
from neuro_code.domain.sandbox import SandboxProfile
from neuro_code.domain.task_dag import TaskDag, TaskDagNode, TaskDagNodeState, TaskDagState
from neuro_code.domain.tools import ToolDefinition
from neuro_code.domain.worktree import (
    WorktreeHandle,
    WorktreeId,
    WorktreeKind,
    WorktreeOwnership,
    WorktreeRepositoryIdentity,
    WorktreeSnapshot,
    WorktreeState,
)
from neuro_code.domain.writable_subagent import (
    WritableSubagentWorkspaceLease,
    WritableSubagentWorkspaceState,
)
from neuro_code.infrastructure.git.worktree import LocalGitWorktreeAdapter
from neuro_code.infrastructure.persistence.sqlite_session import SCHEMA_VERSION, SqliteSessionStore
from neuro_code.infrastructure.tools.filesystem import ExactWorkspaceMutationTool
from neuro_code.infrastructure.workspace.checkpoints import LocalWorkspaceStateAdapter
from neuro_code.infrastructure.workspace.projection import LocalParentWorkspaceProjectionReader
from neuro_code.shared.errors import ToolError

BASE_CONTENT_A = b"base-a\n"
BASE_CONTENT_B = b"base-b\n"
DESIRED_CONTENT_A = b"worker-a\n"
DESIRED_CONTENT_C = b"created-by-worker\n"
EXTERNAL_CONTENT_A = b"edited-by-parent\n"
BASE_SHA = "a" * 40


def _run_git(repository: Path, *arguments: str) -> bytes:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr.decode(errors="replace"))
    return result.stdout


def _crash_after_durable_result_adoption_plan(
    database_path: str,
    adoption_id: str,
    marker_path: str,
) -> None:
    """Fresh spawned controller observes a durable plan, then dies before mutation."""

    async def observe_and_exit() -> None:
        store = SqliteSessionStore(Path(database_path))
        await store.initialize()
        record = await store.get_result_adoption(adoption_id)
        if record is None:
            os._exit(72)
        await asyncio.to_thread(
            _write_durable_marker,
            Path(marker_path),
            record.plan_fingerprint,
        )
        os._exit(73)

    asyncio.run(observe_and_exit())


def _prepare_real_result_adoption_plan_and_exit(
    state_directory: str,
    repository_path: str,
    parent_session_id: str,
    swarm_run_id: str,
    adoption_id: str,
    marker_path: str,
) -> None:
    """Create a real plan, persist it, and die before any parent mutation."""

    async def prepare_and_exit() -> None:
        application = await _open_spawned_composition(Path(state_directory), Path(repository_path))
        binding: ConversationBinding | None = None
        try:
            binding = await application.create_binding(
                resume_id=parent_session_id,
                capabilities=_capability(Path(repository_path), sandbox=SandboxProfile.OFF),
            )
            adoption = application.create_result_adoption_service(parent_binding=binding)
            result = await adoption.prepare(ResultAdoptionRequest(adoption_id, swarm_run_id))
            _write_durable_marker(Path(marker_path), result.plan_fingerprint)
            os._exit(73)
        finally:
            if binding is not None:
                await binding.close()
            await application.close()

    asyncio.run(prepare_and_exit())


def _crash_after_first_result_adoption_mutation(
    state_directory: str,
    repository_path: str,
    parent_session_id: str,
    swarm_run_id: str,
    adoption_id: str,
    marker_path: str,
) -> None:
    """Crash after the first real parent mutation and before its durable ACK."""

    class _CrashMutation:
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        async def apply(
            self,
            request: WorkspaceMutationRequest,
            *,
            session_id: str,
        ) -> WorkspaceMutationResult:
            result = await self._inner.apply(request, session_id=session_id)
            _write_durable_marker(Path(marker_path), request.path)
            os._exit(74)
            return result

    async def mutate_and_exit() -> None:
        os.environ["NEURO_CODE_HOME"] = state_directory
        os.environ["FIXTURE_KEY"] = "fixture-key"
        application = await ApplicationComposition.open(
            ApplicationSettings(
                cwd=Path(repository_path),
                provider="fixture",
                sandbox="off",
                permission_mode=PermissionMode.BYPASS,
                max_steps=8,
            ),
            provider_factory=lambda _config, _failover: cast(ModelProvider, _CompositionProvider()),
        )
        binding: ConversationBinding | None = None
        try:
            binding = await application.create_binding(
                resume_id=parent_session_id,
                capabilities=_capability(Path(repository_path), sandbox=SandboxProfile.OFF),
            )
            if binding.workspace_mutation is None:
                os._exit(71)
            crashing_binding = replace(
                binding,
                workspace_mutation=_CrashMutation(binding.workspace_mutation),
            )
            adoption = application.create_result_adoption_service(
                parent_binding=crashing_binding,
            )
            await adoption.adopt(ResultAdoptionRequest(adoption_id, swarm_run_id))
            os._exit(70)
        finally:
            if binding is not None:
                await binding.close()
            await application.close()

    asyncio.run(mutate_and_exit())


async def _open_spawned_composition(
    state_directory: Path,
    repository_path: Path,
) -> ApplicationComposition:
    os.environ["HOME"] = str(state_directory.parent / "home")
    os.environ["NEURO_CODE_HOME"] = str(state_directory)
    os.environ["FIXTURE_KEY"] = "fixture-key"
    return await ApplicationComposition.open(
        ApplicationSettings(
            cwd=repository_path,
            provider="fixture",
            sandbox="off",
            permission_mode=PermissionMode.BYPASS,
            max_steps=8,
        ),
        provider_factory=lambda _config, _failover: cast(ModelProvider, _CompositionProvider()),
    )


def _write_durable_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())


def _write_durable_json_marker(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="ascii") as stream:
        json.dump(payload, stream, ensure_ascii=True, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def _database_row_counts(state_directory: Path) -> dict[str, int]:
    """Capture durable row counters used by fresh-process no-op proofs."""

    counts: dict[str, int] = {}
    for database_path in (
        state_directory / "sessions.db",
        state_directory / "worktrees.db",
        state_directory / "checkpoints.db",
    ):
        with sqlite3.connect(database_path, timeout=30) as connection:
            table_names = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            for (table_name,) in table_names:
                name = str(table_name)
                quoted = '"' + name.replace('"', '""') + '"'
                counts[f"{database_path.name}:{name}"] = int(
                    connection.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]
                )
    return counts


def _adoption_marker_payload(
    record: Any, calls: Sequence[WorkspaceMutationRequest]
) -> dict[str, object]:
    return {
        "adoption_id": record.adoption_id,
        "plan_fingerprint": record.plan_fingerprint,
        "state": record.state.value,
        "target_states": [target.state.value for target in record.targets],
        "target_versions": [target.version for target in record.targets],
        "call_paths": [call.path for call in calls],
    }


def _crash_after_external_result_adoption_mutation(
    state_directory: str,
    repository_path: str,
    parent_session_id: str,
    swarm_run_id: str,
    adoption_id: str,
    marker_path: str,
) -> None:
    """Simulate a third party edit after durable APPLYING, then crash."""

    class _ExternalMutation:
        async def apply(
            self,
            request: WorkspaceMutationRequest,
            *,
            session_id: str,
        ) -> WorkspaceMutationResult:
            del session_id
            if request.path != "A.txt":
                os._exit(76)
            # This is deliberately outside the production mutation port: it
            # models an unrelated actor changing the parent after APPLYING.
            _write_durable_bytes(Path(repository_path) / "A.txt", EXTERNAL_CONTENT_A)
            _write_durable_marker(Path(marker_path), request.path)
            os._exit(75)
            return WorkspaceMutationResult(request.path, request.operation)

    async def mutate_and_exit() -> None:
        application = await _open_spawned_composition(Path(state_directory), Path(repository_path))
        binding: ConversationBinding | None = None
        try:
            binding = await application.create_binding(
                resume_id=parent_session_id,
                capabilities=_capability(Path(repository_path), sandbox=SandboxProfile.OFF),
            )
            crashing_binding = replace(binding, workspace_mutation=_ExternalMutation())
            adoption = application.create_result_adoption_service(
                parent_binding=crashing_binding,
            )
            await adoption.adopt(ResultAdoptionRequest(adoption_id, swarm_run_id))
            os._exit(70)
        finally:
            if binding is not None:
                await binding.close()
            await application.close()

    asyncio.run(mutate_and_exit())


def _reenter_result_adoption_in_fresh_process(
    state_directory: str,
    repository_path: str,
    parent_session_id: str,
    swarm_run_id: str,
    adoption_id: str,
    marker_path: str,
) -> None:
    """Re-enter one durable adoption from a fresh composition without writes."""

    async def recover() -> None:
        application = await _open_spawned_composition(Path(state_directory), Path(repository_path))
        binding: ConversationBinding | None = None
        try:
            binding = await application.create_binding(
                resume_id=parent_session_id,
                capabilities=_capability(Path(repository_path), sandbox=SandboxProfile.OFF),
            )
            if binding.workspace_mutation is None:
                raise AssertionError("fresh recovery binding has no mutation port")
            mutation = _DelegatingRecordingMutation(binding.workspace_mutation)
            recovery_binding = replace(binding, workspace_mutation=mutation)
            adoption = application.create_result_adoption_service(
                parent_binding=recovery_binding,
            )
            result = await adoption.adopt(ResultAdoptionRequest(adoption_id, swarm_run_id))
            _write_durable_json_marker(
                Path(marker_path),
                _adoption_marker_payload(result, mutation.calls),
            )
        finally:
            if binding is not None:
                await binding.close()
            await application.close()

    asyncio.run(recover())


def _complete_result_adoption_in_fresh_process(
    state_directory: str,
    repository_path: str,
    parent_session_id: str,
    swarm_run_id: str,
    adoption_id: str,
    marker_path: str,
) -> None:
    """Complete one real adoption in an OS process that then exits normally."""

    async def complete() -> None:
        application = await _open_spawned_composition(Path(state_directory), Path(repository_path))
        binding: ConversationBinding | None = None
        try:
            binding = await application.create_binding(
                resume_id=parent_session_id,
                capabilities=_capability(Path(repository_path), sandbox=SandboxProfile.OFF),
            )
            adoption = application.create_result_adoption_service(parent_binding=binding)
            result = await adoption.adopt(ResultAdoptionRequest(adoption_id, swarm_run_id))
            if result.state is not ResultAdoptionState.COMPLETED:
                raise AssertionError(f"fresh adoption did not complete: {result.state}")
            _write_durable_json_marker(
                Path(marker_path),
                _adoption_marker_payload(result, ()),
            )
        finally:
            if binding is not None:
                await binding.close()
            await application.close()

    asyncio.run(complete())


def _write_durable_marker(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="ascii") as stream:
        stream.write(value + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _entry(
    path: str,
    content: bytes,
    *,
    scope: WorkspaceFileScope = WorkspaceFileScope.TRACKED,
) -> WorkspaceFileEntry:
    return WorkspaceFileEntry(
        path=path,
        scope=scope,
        present=True,
        kind=WorkspaceFileKind.REGULAR,
        mode=0o100644,
        content=content,
    )


def _capability(cwd: Path, *, sandbox: SandboxProfile) -> SubagentCapabilitySet:
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
        cwd=cwd,
        sandbox_profile=sandbox,
        enable_background_tasks=False,
        max_steps=8,
    )


def _projection(
    head_sha: str,
    entries: list[WorkspaceFileEntry],
    *,
    branch: str | None,
    detached: bool,
) -> WorkspaceProjection:
    return WorkspaceProjection(
        head_sha=head_sha,
        branch=branch,
        detached=detached,
        index_bytes=b"index",
        entries=tuple(sorted(entries, key=lambda item: (item.path, item.scope.value))),
    )


class _ParentRunner:
    def __init__(self, session_id: str) -> None:
        self._session_id = session_id

    @property
    def session_id(self) -> str:
        return self._session_id


class _MutableParent:
    def __init__(
        self,
        repository: WorktreeRepositoryIdentity,
        entries: list[WorkspaceFileEntry],
    ) -> None:
        self.repository = repository
        self.entries = {entry.path: entry for entry in entries}

    def current(self, path: str) -> WorkspaceFileEntry | None:
        return self.entries.get(path)

    def set_entry(self, entry: WorkspaceFileEntry | None, *, path: str) -> None:
        if entry is None:
            self.entries.pop(path, None)
        else:
            self.entries[path] = entry

    def snapshot(self) -> ParentWorkspaceSnapshot:
        return ParentWorkspaceSnapshot(
            repository=self.repository,
            projection=_projection(
                self.repository.head_sha,
                list(self.entries.values()),
                branch="main",
                detached=False,
            ),
        )

    async def inspect(self, root: Path, /) -> ParentWorkspaceSnapshot:
        assert root == self.repository.source_worktree
        return self.snapshot()


class _RecordingMutation:
    def __init__(self, parent: _MutableParent) -> None:
        self.parent = parent
        self.calls: list[WorkspaceMutationRequest] = []

    async def apply(
        self,
        request: WorkspaceMutationRequest,
        *,
        session_id: str,
    ) -> WorkspaceMutationResult:
        assert session_id
        assert self.parent.current(request.path) == request.expected
        self.calls.append(request)
        self.parent.set_entry(request.desired, path=request.path)
        return WorkspaceMutationResult(request.path, request.operation)


class _FailOnceMutation(_RecordingMutation):
    def __init__(self, parent: _MutableParent) -> None:
        super().__init__(parent)
        self.fail_once = True

    async def apply(
        self,
        request: WorkspaceMutationRequest,
        *,
        session_id: str,
    ) -> WorkspaceMutationResult:
        if self.fail_once:
            self.fail_once = False
            raise OSError("temporary mutation failure")
        return await super().apply(request, session_id=session_id)


class _PermissionDeniedMutation:
    def __init__(self) -> None:
        self.calls: list[WorkspaceMutationRequest] = []

    async def apply(
        self,
        request: WorkspaceMutationRequest,
        *,
        session_id: str,
    ) -> WorkspaceMutationResult:
        assert session_id
        self.calls.append(request)
        raise OSError("permission denied")


class _ApplyThenFailMutation(_RecordingMutation):
    def __init__(self, parent: _MutableParent) -> None:
        super().__init__(parent)
        self.fail_after_apply = True

    async def apply(
        self,
        request: WorkspaceMutationRequest,
        *,
        session_id: str,
    ) -> WorkspaceMutationResult:
        if self.fail_after_apply:
            self.fail_after_apply = False
            assert session_id
            assert self.parent.current(request.path) == request.expected
            self.calls.append(request)
            self.parent.set_entry(request.desired, path=request.path)
            raise OSError("mutation acknowledgement failed after write")
        return await super().apply(request, session_id=session_id)


class _NoopOnceMutation(_RecordingMutation):
    def __init__(self, parent: _MutableParent) -> None:
        super().__init__(parent)
        self.noop_once = True

    async def apply(
        self,
        request: WorkspaceMutationRequest,
        *,
        session_id: str,
    ) -> WorkspaceMutationResult:
        if self.noop_once:
            self.noop_once = False
            assert session_id
            assert self.parent.current(request.path) == request.expected
            self.calls.append(request)
            return WorkspaceMutationResult(request.path, request.operation)
        return await super().apply(request, session_id=session_id)


class _FailingParentReader:
    def __init__(self, parent: _MutableParent, *, fail_on: int) -> None:
        self.parent = parent
        self.fail_on = fail_on
        self.calls = 0

    async def inspect(self, root: Path, /) -> ParentWorkspaceSnapshot:
        self.calls += 1
        if self.calls == self.fail_on:
            raise OSError("parent projection unavailable")
        return await self.parent.inspect(root)


class _FinalVerificationRaceReader:
    def __init__(self, parent: _MutableParent) -> None:
        self.parent = parent
        self.calls = 0

    async def inspect(self, root: Path, /) -> ParentWorkspaceSnapshot:
        self.calls += 1
        if self.calls == 8:
            self.parent.set_entry(_entry("A.txt", EXTERNAL_CONTENT_A), path="A.txt")
        return await self.parent.inspect(root)


class _DelegatingRecordingMutation:
    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.calls: list[WorkspaceMutationRequest] = []

    async def apply(
        self,
        request: WorkspaceMutationRequest,
        *,
        session_id: str,
    ) -> WorkspaceMutationResult:
        self.calls.append(request)
        return await self._inner.apply(request, session_id=session_id)


class _GraphStore:
    def __init__(
        self,
        result_store: SqliteSessionStore,
        swarm: AgentSwarmRun,
        dag: TaskDag,
        leases: dict[str, WritableSubagentWorkspaceLease],
    ) -> None:
        self._result_store = result_store
        self.swarm = swarm
        self.dag = dag
        self.leases = leases

    def __getattr__(self, name: str) -> Any:
        return getattr(self._result_store, name)

    async def get_swarm_run(self, swarm_run_id: str, /) -> AgentSwarmRun | None:
        return self.swarm if swarm_run_id == self.swarm.swarm_run_id else None

    async def get_task_dag(self, dag_id: str, /) -> TaskDag | None:
        return self.dag if dag_id == self.dag.dag_id else None

    async def get_writable_subagent_lease(
        self,
        lease_id: str,
        /,
    ) -> WritableSubagentWorkspaceLease | None:
        return self.leases.get(lease_id)


class _Worktrees:
    def __init__(self, snapshots: dict[str, WorktreeSnapshot]) -> None:
        self.snapshots = snapshots

    async def initialize(self) -> None:
        return None

    async def inspect(self, worktree_id: str, /) -> WorktreeSnapshot:
        return self.snapshots[worktree_id]


class _Checkpoints:
    def __init__(
        self,
        checkpoints: dict[str, WorkspaceCheckpoint],
        baselines: dict[str, WorkspaceProjection],
        live: dict[str, WorkspaceProjection],
    ) -> None:
        self.checkpoints = checkpoints
        self.baselines = baselines
        self.live = live

    async def initialize(self) -> None:
        return None

    async def get(self, checkpoint_id: CheckpointId, /) -> WorkspaceCheckpoint | None:
        return self.checkpoints.get(checkpoint_id.value)

    async def load_projection(self, checkpoint_id: CheckpointId, /) -> WorkspaceProjection:
        return self.baselines[checkpoint_id.value]

    async def inspect(self, handle: WorktreeHandle, /) -> WorkspaceProjection:
        return self.live[handle.worktree_id.value]


@dataclass
class _Fixture:
    store: SqliteSessionStore
    graph: _GraphStore
    worktrees: _Worktrees
    checkpoints: _Checkpoints
    parent: _MutableParent
    mutation: _RecordingMutation
    binding: ConversationBinding
    request: ResultAdoptionRequest
    service: ResultAdoptionApplicationService

    def new_service(self) -> ResultAdoptionApplicationService:
        return ResultAdoptionApplicationService(
            store=cast(Any, self.graph),
            swarms=cast(Any, self.graph),
            dags=cast(TaskDagStore, self.graph),
            leases=cast(WritableSubagentLeaseStore, self.graph),
            worktrees=self.worktrees,
            checkpoints=self.checkpoints,
            parent_reader=self.parent,
            mutation=self.mutation,
            parent_binding=self.binding,
        )


async def _make_fixture(
    tmp_path: Path,
    *,
    overlap: bool = False,
    parent_conflict: bool = False,
    stale_worker: bool = False,
) -> _Fixture:
    os.makedirs(tmp_path, exist_ok=True)
    repository_path = tmp_path / "parent"
    repository_path.mkdir()
    _run_git(repository_path, "init", "-q")
    _run_git(repository_path, "config", "user.email", "neuro-code-tests@example.invalid")
    _run_git(repository_path, "config", "user.name", "Neuro Code Tests")
    (repository_path / "A.txt").write_bytes(BASE_CONTENT_A)
    (repository_path / "B.txt").write_bytes(BASE_CONTENT_B)
    _run_git(repository_path, "add", "A.txt", "B.txt")
    _run_git(repository_path, "commit", "-qm", "initial")
    head_sha = _run_git(repository_path, "rev-parse", "HEAD").decode().strip()
    repository = WorktreeRepositoryIdentity(
        common_dir=repository_path / ".git",
        source_worktree=repository_path,
        git_dir=repository_path / ".git",
        head_sha=head_sha,
    )
    (repository_path / "U.txt").write_bytes(b"unrelated dirty\n")
    parent_entries = [
        _entry("A.txt", EXTERNAL_CONTENT_A if parent_conflict else BASE_CONTENT_A),
        _entry("B.txt", BASE_CONTENT_B),
        _entry("U.txt", b"unrelated dirty\n", scope=WorkspaceFileScope.UNTRACKED),
    ]
    parent = _MutableParent(repository, parent_entries)
    mutation = _RecordingMutation(parent)

    state_directory = tmp_path / "state"
    store = SqliteSessionStore(state_directory / "sessions.db")
    await store.initialize()
    parent_session_id = await store.create_session(
        str(repository_path),
        "fixture-provider",
        "fixture-model",
    )
    capabilities = _capability(repository_path, sandbox=SandboxProfile.WORKSPACE)
    binding = ConversationBinding(
        cast(ConversationRunner, _ParentRunner(parent_session_id)),
        cast(ModelProvider, object()),
        capabilities=capabilities,
        workspace_root=repository_path,
        workspace_mutation=mutation,
    )

    now = datetime(2026, 8, 28, tzinfo=UTC)
    baselines: dict[str, WorkspaceProjection] = {}
    live_projections: dict[str, WorkspaceProjection] = {}
    snapshots: dict[str, WorktreeSnapshot] = {}
    checkpoints: dict[str, WorkspaceCheckpoint] = {}
    leases: dict[str, WritableSubagentWorkspaceLease] = {}
    nodes: list[TaskDagNode] = []
    for ordinal in range(2):
        worktree_id = WorktreeId(f"wt-worker-{ordinal}")
        child_root = state_directory / "worktrees" / worktree_id.value
        child_root.mkdir(parents=True)
        handle = WorktreeHandle(
            worktree_id=worktree_id,
            repository=repository,
            path=child_root,
            base_commit_sha=head_sha,
            branch=None,
        )
        snapshot = WorktreeSnapshot(
            worktree_id=worktree_id,
            repository=repository,
            canonical_path=child_root,
            base_revision=head_sha,
            base_commit_sha=head_sha,
            branch=None,
            kind=WorktreeKind.DETACHED,
            ownership=WorktreeOwnership.MANAGED,
            state=WorktreeState.READY,
            created_at=now,
        )
        baseline = _projection(
            head_sha,
            [_entry("A.txt", BASE_CONTENT_A), _entry("B.txt", BASE_CONTENT_B)],
            branch=None,
            detached=True,
        )
        if ordinal == 0:
            live = _projection(
                head_sha,
                [_entry("A.txt", DESIRED_CONTENT_A), _entry("B.txt", BASE_CONTENT_B)],
                branch=None,
                detached=True,
            )
        elif overlap:
            live = _projection(
                head_sha,
                [_entry("A.txt", b"worker-b\n"), _entry("B.txt", BASE_CONTENT_B)],
                branch=None,
                detached=True,
            )
        else:
            live = _projection(
                head_sha,
                [
                    _entry("A.txt", BASE_CONTENT_A),
                    _entry("B.txt", BASE_CONTENT_B),
                    _entry("C.txt", DESIRED_CONTENT_C, scope=WorkspaceFileScope.UNTRACKED),
                ],
                branch=None,
                detached=True,
            )
        final_fingerprint = workspace_projection_fingerprint(handle, live).value
        live_projections[worktree_id.value] = live
        snapshots[worktree_id.value] = snapshot
        checkpoint_id = CheckpointId(f"cp-worker-{ordinal}")
        baselines[checkpoint_id.value] = baseline
        checkpoints[checkpoint_id.value] = WorkspaceCheckpoint(
            checkpoint_id=checkpoint_id,
            worktree_id=worktree_id,
            repository=repository,
            canonical_path=child_root,
            head_sha=head_sha,
            branch=None,
            detached=True,
            created_at=now,
            source_fingerprint=workspace_projection_fingerprint(handle, baseline),
            artifact_path=state_directory / f"{checkpoint_id.value}.json",
            artifact_sha256="3" * 64,
            artifact_bytes=1,
            artifact_file_count=len(baseline.entries),
            state=CheckpointState.READY,
        )
        lease_id = f"lease-worker-{ordinal}"
        lease = WritableSubagentWorkspaceLease(
            lease_id=lease_id,
            parent_session_id=parent_session_id,
            parent_task_id=f"task-worker-{ordinal}",
            worktree_id=worktree_id,
            parent_capability_fingerprint=capabilities.fingerprint,
            parent_workspace_root=repository_path,
            parent_repository=repository,
            base_commit_sha=head_sha,
            canonical_child_root=child_root,
            state=WritableSubagentWorkspaceState.PRESERVED,
            created_at=now,
            updated_at=now,
            worktree=handle,
            baseline_checkpoint_id=checkpoint_id,
            child_session_id=f"child-worker-{ordinal}",
            capability_fingerprint="1" * 64,
            grant_fingerprint="2" * 64,
            owner_pid=1,
            owner_token=f"worker-owner-{ordinal}",
            final_workspace_fingerprint=final_fingerprint,
            workspace_changed=True,
            changed_file_count=1,
        )
        leases[lease_id] = lease
        nodes.append(
            TaskDagNode(
                node_id=f"node-worker-{ordinal}",
                ordinal=ordinal,
                prompt=f"worker {ordinal}",
                state=TaskDagNodeState.COMPLETED,
                generation=1,
                parent_task_id=lease.parent_task_id,
                child_session_id=lease.child_session_id,
                lease_id=lease.lease_id,
                worktree_id=worktree_id.value,
                baseline_checkpoint_id=checkpoint_id.value,
                relay_id=f"relay-worker-{ordinal}",
                final_workspace_fingerprint=final_fingerprint,
                changed_file_count=1,
            )
        )
    if stale_worker:
        live_projections["wt-worker-0"] = _projection(
            head_sha,
            [_entry("A.txt", b"changed-after-preservation\n"), _entry("B.txt", BASE_CONTENT_B)],
            branch=None,
            detached=True,
        )

    dag = TaskDag(
        dag_id="dag-result-adoption",
        parent_session_id=parent_session_id,
        nodes=tuple(nodes),
        state=TaskDagState.COMPLETED,
        generation=0,
        created_at=now,
        updated_at=now,
    )
    response = "completed"
    swarm = AgentSwarmRun(
        swarm_run_id="swarm-result-adoption",
        parent_session_id=parent_session_id,
        objective_fingerprint=objective_fingerprint("adopt worker results"),
        planning_id="planning-result-adoption",
        state=AgentSwarmRunState.COMPLETED,
        generation=0,
        owner_id="swarm-owner",
        owner_pid=1,
        owner_token="swarm-owner-token",
        lease_expires_at=now + timedelta(minutes=5),
        created_at=now,
        updated_at=now,
        root_dag_id=dag.dag_id,
        current_dag_id=dag.dag_id,
        current_dag_generation=dag.generation,
        current_dag_definition_fingerprint=dag.definition_fingerprint,
        final_response=response,
        final_result_fingerprint=terminal_result_fingerprint(
            "swarm-result-adoption",
            dag.dag_id,
            dag.generation,
            dag.definition_fingerprint,
            response,
        ),
    )
    graph = _GraphStore(store, swarm, dag, leases)
    worktrees = _Worktrees(snapshots)
    checkpoints_service = _Checkpoints(checkpoints, baselines, live_projections)
    request = ResultAdoptionRequest("adopt-result-adoption", swarm.swarm_run_id)
    service = ResultAdoptionApplicationService(
        store=cast(Any, graph),
        swarms=cast(Any, graph),
        dags=cast(TaskDagStore, graph),
        leases=cast(WritableSubagentLeaseStore, graph),
        worktrees=worktrees,
        checkpoints=checkpoints_service,
        parent_reader=parent,
        mutation=mutation,
        parent_binding=binding,
    )
    if stale_worker:
        # The lease keeps the original final fingerprint; only the live source
        # projection is changed, modelling a worker directory modified after
        # it was durably preserved.
        assert live_projections["wt-worker-0"] != baselines["cp-worker-0"]
    return _Fixture(
        store,
        graph,
        worktrees,
        checkpoints_service,
        parent,
        mutation,
        binding,
        request,
        service,
    )


@pytest.mark.asyncio
async def test_adoption_is_three_way_idempotent_and_preserves_unrelated_dirty_files(
    tmp_path: Path,
) -> None:
    fixture = await _make_fixture(tmp_path)

    result = await fixture.service.adopt(fixture.request)

    assert result.state is ResultAdoptionState.COMPLETED
    assert [call.path for call in fixture.mutation.calls] == ["A.txt", "C.txt"]
    assert fixture.parent.current("A.txt") == _entry("A.txt", DESIRED_CONTENT_A)
    assert fixture.parent.current("B.txt") == _entry("B.txt", BASE_CONTENT_B)
    assert fixture.parent.current("C.txt") == _entry(
        "C.txt", DESIRED_CONTENT_C, scope=WorkspaceFileScope.UNTRACKED
    )
    assert fixture.parent.current("U.txt") == _entry(
        "U.txt", b"unrelated dirty\n", scope=WorkspaceFileScope.UNTRACKED
    )

    recovered = await fixture.new_service().adopt(fixture.request)
    assert recovered == result
    assert len(fixture.mutation.calls) == 2
    persisted = await fixture.store.get_result_adoption(fixture.request.adoption_id)
    assert persisted == result
    assert all(target.state is ResultAdoptionTargetState.APPLIED for target in persisted.targets)


@pytest.mark.asyncio
async def test_result_adoption_plan_round_trips_all_durable_images(tmp_path: Path) -> None:
    fixture = await _make_fixture(tmp_path)

    prepared = await fixture.service.prepare(fixture.request)
    restored = ResultAdoptionPlan.from_dict(prepared.plan.to_dict())

    assert restored == prepared.plan
    assert all(
        ResultAdoptionTarget.from_dict(target.to_dict()) == target
        for target in prepared.plan.targets
    )
    assert all(
        type(source).from_dict(source.to_dict()) == source for source in prepared.plan.sources
    )


@pytest.mark.asyncio
async def test_result_adoption_rejects_invalid_runtime_configuration_and_live_owner(
    tmp_path: Path,
) -> None:
    fixture = await _make_fixture(tmp_path)

    def make_service(
        *,
        binding: Any = fixture.binding,
        mutation: Any = fixture.mutation,
        parent_reader: Any = fixture.parent,
        lease_seconds: Any = MAX_RESULT_ADOPTION_LEASE_SECONDS,
    ) -> ResultAdoptionApplicationService:
        return ResultAdoptionApplicationService(
            store=cast(Any, fixture.graph),
            swarms=cast(Any, fixture.graph),
            dags=cast(TaskDagStore, fixture.graph),
            leases=cast(WritableSubagentLeaseStore, fixture.graph),
            worktrees=fixture.worktrees,
            checkpoints=fixture.checkpoints,
            parent_reader=cast(Any, parent_reader),
            mutation=cast(Any, mutation),
            parent_binding=cast(ConversationBinding, binding),
            lease_seconds=cast(float, lease_seconds),
        )

    with pytest.raises(ResultAdoptionError, match="parent binding is required"):
        make_service(binding=object())
    with pytest.raises(ResultAdoptionError, match="parent session is unavailable"):
        make_service(
            binding=replace(
                fixture.binding,
                runner=cast(ConversationRunner, _ParentRunner("")),
            )
        )
    with pytest.raises(ResultAdoptionError, match="workspace root is unavailable"):
        make_service(binding=replace(fixture.binding, workspace_root=None))
    with pytest.raises(ResultAdoptionError, match="capability metadata is missing"):
        make_service(binding=replace(fixture.binding, capabilities=None))
    with pytest.raises(ResultAdoptionError, match="does not carry writable authority"):
        make_service(
            binding=replace(
                fixture.binding,
                capabilities=_capability(
                    fixture.parent.repository.source_worktree,
                    sandbox=SandboxProfile.READ_ONLY,
                ),
            )
        )
    with pytest.raises(ResultAdoptionError, match="mutation port is unavailable"):
        make_service(mutation=object())
    with pytest.raises(ResultAdoptionError, match="parent reader is unavailable"):
        make_service(parent_reader=object())
    with pytest.raises(ValueError, match="lease duration is invalid"):
        make_service(lease_seconds=True)
    with pytest.raises(ValueError, match="lease duration is out of bounds"):
        make_service(lease_seconds=MAX_RESULT_ADOPTION_LEASE_SECONDS + 1)

    await fixture.service.prepare(fixture.request)
    with pytest.raises(ResultAdoptionError, match="another live controller") as busy:
        await fixture.new_service().adopt(fixture.request)
    assert busy.value.kind == "busy"
    assert fixture.mutation.calls == []


@pytest.mark.asyncio
async def test_schema_28_to_29_result_adoption_migration_is_idempotent_and_lossless(
    tmp_path: Path,
) -> None:
    database = tmp_path / "sessions.db"
    store = SqliteSessionStore(database)
    await store.initialize()
    session_id = await store.create_session(str(tmp_path), "fixture-provider", "fixture-model")

    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("DROP TABLE result_adoption_targets")
        connection.execute("DROP TABLE result_adoptions")
        connection.execute("UPDATE schema_meta SET version = 28 WHERE singleton = 1")

    migrated = SqliteSessionStore(database)
    await migrated.initialize()
    assert SCHEMA_VERSION == 29
    assert await migrated.get_session(session_id) is not None

    def schema_snapshot() -> tuple[tuple[str, str | None], ...]:
        with closing(sqlite3.connect(database)) as connection:
            assert connection.execute(
                "SELECT version FROM schema_meta WHERE singleton = 1"
            ).fetchone() == (29,)
            return tuple(
                (str(row[0]), None if row[1] is None else str(row[1]))
                for row in connection.execute(
                    "SELECT name, sql FROM sqlite_master WHERE type = 'table' "
                    "AND name IN ('result_adoptions', 'result_adoption_targets') ORDER BY name"
                ).fetchall()
            )

    first_schema = schema_snapshot()
    assert [name for name, _sql in first_schema] == [
        "result_adoption_targets",
        "result_adoptions",
    ]
    with closing(sqlite3.connect(database)) as connection:
        adoption_columns = {
            str(row[1]): int(row[5])
            for row in connection.execute("PRAGMA table_info(result_adoptions)").fetchall()
        }
        target_columns = {
            str(row[1]): int(row[5])
            for row in connection.execute("PRAGMA table_info(result_adoption_targets)").fetchall()
        }
        assert adoption_columns["adoption_id"] == 1
        assert target_columns["adoption_id"] == 1
        assert target_columns["ordinal"] == 2
        assert connection.execute("SELECT COUNT(*) FROM result_adoptions").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM result_adoption_targets").fetchone() == (0,)

    await migrated.initialize()
    assert schema_snapshot() == first_schema


@pytest.mark.asyncio
async def test_result_adoption_domain_rejects_invalid_boundaries(tmp_path: Path) -> None:
    fixture = await _make_fixture(tmp_path)
    prepared = await fixture.service.prepare(fixture.request)
    source = prepared.plan.sources[0]
    target = prepared.plan.targets[0]

    with pytest.raises(ValueError, match="adopt- prefix"):
        ResultAdoptionRequest("invalid", prepared.plan.swarm_run_id)
    with pytest.raises(ValueError, match="safe identifier"):
        ResultAdoptionRequest("adopt-invalid", "\x00")
    with pytest.raises(TypeError, match="worktree id"):
        replace(source, worktree_id="not-a-worktree")
    with pytest.raises(TypeError, match="checkpoint id"):
        replace(source, baseline_checkpoint_id="not-a-checkpoint")
    with pytest.raises(ValueError, match="Git SHA"):
        replace(source, base_commit_sha="not-a-commit")
    with pytest.raises(TypeError, match="repository"):
        replace(source, parent_repository=object())
    with pytest.raises(ValueError, match="SHA-256"):
        replace(source, capability_fingerprint="not-a-digest")
    with pytest.raises(ValueError, match="bounded relative"):
        ResultAdoptionTarget("\x00bad", target.operation, target.baseline, target.desired)
    with pytest.raises(ValueError, match="traversal"):
        ResultAdoptionTarget("../escape", target.operation, target.baseline, target.desired)
    with pytest.raises(TypeError, match="operation"):
        replace(target, operation="update")
    with pytest.raises(ValueError, match="does not match"):
        ResultAdoptionTarget(
            target.path,
            ResultAdoptionOperation.UPDATE,
            _entry("other.txt", BASE_CONTENT_A),
            target.desired,
        )
    with pytest.raises(ValueError, match="parent workspace"):
        replace(prepared.plan, parent_workspace_root=tmp_path / "other-root")
    with pytest.raises(TypeError, match=r"pathlib\.Path"):
        replace(prepared.plan, parent_workspace_root="not-a-path")
    with pytest.raises(ValueError, match="parent HEAD"):
        replace(prepared.plan, parent_head_sha="b" * 40)
    with pytest.raises(ValueError, match="DAG generation"):
        replace(prepared.plan, dag_generation=True)
    with pytest.raises(ValueError, match="source count"):
        replace(prepared.plan, sources=())
    with pytest.raises(ValueError, match="source nodes"):
        replace(prepared.plan, sources=(source, source))
    with pytest.raises(ValueError, match="target paths"):
        replace(prepared.plan, targets=(target, target))
    with pytest.raises(ValueError, match="timezone-aware"):
        replace(prepared.plan, created_at=datetime.fromisoformat("2026-08-28T00:00:00"))
    with pytest.raises(ValueError, match="adopt- prefix"):
        replace(prepared.plan, adoption_id="invalid")
    with pytest.raises(TypeError, match="repository"):
        replace(prepared.plan, parent_repository=object())
    with pytest.raises(ValueError, match="source count"):
        replace(prepared.plan, sources=(source,) * 9)
    with pytest.raises(ValueError, match="target count"):
        replace(prepared.plan, targets=(target,) * 65)
    with pytest.raises(TypeError, match="sources must be canonical"):
        replace(prepared.plan, sources=(object(),))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="targets must be canonical"):
        replace(prepared.plan, targets=(object(),))  # type: ignore[arg-type]
    mismatched_source = replace(
        source,
        parent_repository=replace(source.parent_repository, head_sha="b" * 40),
    )
    with pytest.raises(ValueError, match="exact parent repository"):
        replace(prepared.plan, sources=(mismatched_source,))

    with pytest.raises(ValueError, match="plan is invalid"):
        ResultAdoptionPlan.from_dict(None)
    invalid_plan = prepared.plan.to_dict()
    invalid_plan["sources"] = "not-a-list"
    with pytest.raises(ValueError, match="source/target lists"):
        ResultAdoptionPlan.from_dict(invalid_plan)
    invalid_plan = prepared.plan.to_dict()
    invalid_plan["dag_generation"] = True
    with pytest.raises(ValueError, match="DAG generation is invalid"):
        ResultAdoptionPlan.from_dict(invalid_plan)
    invalid_plan = prepared.plan.to_dict()
    invalid_plan["parent_repository"] = None
    with pytest.raises(ValueError, match="repository identity is invalid"):
        ResultAdoptionPlan.from_dict(invalid_plan)

    with pytest.raises(ValueError, match="source is invalid"):
        type(source).from_dict(None)
    invalid_source = source.to_dict()
    invalid_source["parent_repository"] = None
    with pytest.raises(ValueError, match="repository identity is invalid"):
        type(source).from_dict(invalid_source)
    invalid_source = source.to_dict()
    invalid_source["parent_repository"] = {
        "common_dir": 1,
        "source_worktree": str(source.parent_repository.source_worktree),
        "git_dir": str(source.parent_repository.git_dir),
        "head_sha": source.parent_repository.head_sha,
    }
    with pytest.raises(ValueError, match="repository identity fields"):
        type(source).from_dict(invalid_source)

    with pytest.raises(ValueError, match="target is invalid"):
        type(target).from_dict(None)
    invalid_target = target.to_dict()
    invalid_target["operation"] = 1
    with pytest.raises(ValueError, match="target operation is invalid"):
        type(target).from_dict(invalid_target)
    invalid_target = target.to_dict()
    invalid_target["baseline"] = object()
    with pytest.raises(ValueError, match="workspace image is invalid"):
        type(target).from_dict(invalid_target)
    invalid_target = target.to_dict()
    invalid_target["baseline"] = {"path": target.path, "present": True, "mode": 0o100644}
    with pytest.raises(ValueError, match="workspace image identity"):
        type(target).from_dict(invalid_target)
    invalid_target = target.to_dict()
    baseline_payload = cast(dict[str, object], invalid_target["baseline"])
    baseline_payload["content_b64"] = 1
    with pytest.raises(ValueError, match="content encoding is invalid"):
        type(target).from_dict(invalid_target)
    invalid_target = target.to_dict()
    baseline_payload = cast(dict[str, object], invalid_target["baseline"])
    baseline_payload["content_b64"] = "%"
    with pytest.raises(ValueError, match="content encoding is invalid"):
        type(target).from_dict(invalid_target)

    with pytest.raises(TypeError, match="operation"):
        ResultAdoptionTarget(
            target.path,
            "update",  # type: ignore[arg-type]
            target.baseline,
            target.desired,
        )


@pytest.mark.asyncio
async def test_result_adoption_ports_reject_noncanonical_records(tmp_path: Path) -> None:
    fixture = await _make_fixture(tmp_path)
    prepared = await fixture.service.prepare(fixture.request)
    target_record = prepared.targets[0]
    aware = prepared.updated_at

    error = ResultAdoptionError("x" * 2_000, kind="bounded")
    assert error.kind == "bounded"
    assert len(str(error)) == 1_000

    with pytest.raises(TypeError, match="repository"):
        ParentWorkspaceSnapshot(object(), prepared.plan.targets)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="projection"):
        ParentWorkspaceSnapshot(prepared.plan.parent_repository, object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="non-empty"):
        WorkspaceMutationRequest(
            "",
            ResultAdoptionOperation.UPDATE,
            target_record.target.baseline,
            target_record.target.desired,
        )
    with pytest.raises(TypeError, match="operation"):
        WorkspaceMutationRequest(
            "A.txt",
            "update",
            target_record.target.baseline,
            target_record.target.desired,  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError, match="expected image"):
        WorkspaceMutationRequest(
            "A.txt",
            ResultAdoptionOperation.UPDATE,
            object(),  # type: ignore[arg-type]
            target_record.target.desired,
        )
    with pytest.raises(TypeError, match="desired image"):
        WorkspaceMutationRequest(
            "A.txt",
            ResultAdoptionOperation.UPDATE,
            target_record.target.baseline,
            object(),  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="expected path"):
        WorkspaceMutationRequest(
            "A.txt",
            ResultAdoptionOperation.UPDATE,
            _entry("other.txt", BASE_CONTENT_A),
            target_record.target.desired,
        )
    with pytest.raises(ValueError, match="desired path"):
        WorkspaceMutationRequest(
            "A.txt",
            ResultAdoptionOperation.UPDATE,
            target_record.target.baseline,
            _entry("other.txt", DESIRED_CONTENT_A),
        )
    with pytest.raises(ValueError, match="non-empty"):
        WorkspaceMutationResult("", ResultAdoptionOperation.UPDATE)
    with pytest.raises(TypeError, match="operation"):
        WorkspaceMutationResult("A.txt", "update")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="target record target"):
        ResultAdoptionTargetRecord(object(), ResultAdoptionTargetState.NOT_STARTED)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="target record state"):
        ResultAdoptionTargetRecord(target_record.target, "not-started")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="observed fingerprint"):
        replace(target_record, observed_fingerprint="not-a-digest")
    with pytest.raises(ValueError, match="error kind"):
        replace(target_record, error_kind="")
    with pytest.raises(ValueError, match="timezone-aware"):
        replace(target_record, updated_at=datetime.fromisoformat("2026-08-28T00:00:00"))
    with pytest.raises(ValueError, match="timezone-aware"):
        replace(prepared, lease_expires_at=datetime.fromisoformat("2026-08-28T00:00:00"))
    with pytest.raises(ValueError, match="version"):
        replace(target_record, version=-1)
    with pytest.raises(TypeError, match="record plan"):
        replace(prepared, plan=object())
    with pytest.raises(TypeError, match="record state"):
        replace(prepared, state="claimed")
    with pytest.raises(ValueError, match="owner pid"):
        replace(prepared, owner_pid=0)
    with pytest.raises(ValueError, match="owner token"):
        replace(prepared, owner_token="")
    with pytest.raises(ValueError, match="update time"):
        replace(prepared, updated_at=aware - timedelta(seconds=1))
    with pytest.raises(ValueError, match="target records"):
        replace(prepared, targets=())
    with pytest.raises(TypeError, match="target records"):
        replace(prepared, targets=(*prepared.targets[:-1], object()))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="out of order"):
        replace(prepared, targets=tuple(reversed(prepared.targets)))
    with pytest.raises(ValueError, match="error kind"):
        replace(prepared, error_kind="")
    with pytest.raises(ValueError, match="version"):
        replace(prepared, version=-1)
    assert prepared.applied_paths == ()


@pytest.mark.asyncio
async def test_result_adoption_rejects_unusable_durable_sources_before_persisting(
    tmp_path: Path,
) -> None:
    def missing_swarm(fixture: _Fixture) -> None:
        swarm = fixture.graph.swarm
        fixture.graph.swarm = replace(
            swarm,
            swarm_run_id="different-swarm",
            final_result_fingerprint=terminal_result_fingerprint(
                "different-swarm",
                swarm.current_dag_id,
                swarm.current_dag_generation,
                swarm.current_dag_definition_fingerprint,
                swarm.final_response,
            ),
        )

    def unfinished_swarm(fixture: _Fixture) -> None:
        fixture.graph.swarm = replace(fixture.graph.swarm, state=AgentSwarmRunState.PLANNING)

    def wrong_swarm_parent(fixture: _Fixture) -> None:
        fixture.graph.swarm = replace(fixture.graph.swarm, parent_session_id="other-parent")

    def missing_swarm_dag_identity(fixture: _Fixture) -> None:
        fixture.graph.swarm = replace(
            fixture.graph.swarm,
            current_dag_generation=None,
            current_dag_definition_fingerprint=None,
        )

    def missing_dag(fixture: _Fixture) -> None:
        fixture.graph.dag = replace(fixture.graph.dag, dag_id="different-dag")

    def stale_dag(fixture: _Fixture) -> None:
        fixture.graph.dag = replace(fixture.graph.dag, state=TaskDagState.RUNNING)

    def failed_node(fixture: _Fixture) -> None:
        node = replace(fixture.graph.dag.nodes[0], state=TaskDagNodeState.FAILED)
        fixture.graph.dag = replace(fixture.graph.dag, nodes=(node, *fixture.graph.dag.nodes[1:]))

    def incomplete_node_linkage(fixture: _Fixture) -> None:
        node = replace(fixture.graph.dag.nodes[0], final_workspace_fingerprint=None)
        fixture.graph.dag = replace(fixture.graph.dag, nodes=(node, *fixture.graph.dag.nodes[1:]))

    def missing_lease(fixture: _Fixture) -> None:
        del fixture.graph.leases["lease-worker-0"]

    def unfinished_lease(fixture: _Fixture) -> None:
        lease = replace(
            fixture.graph.leases["lease-worker-0"],
            state=WritableSubagentWorkspaceState.FAILED,
        )
        fixture.graph.leases[lease.lease_id] = lease

    def mismatched_lease(fixture: _Fixture) -> None:
        lease = replace(
            fixture.graph.leases["lease-worker-0"],
            parent_task_id="different-task",
        )
        fixture.graph.leases[lease.lease_id] = lease

    def unavailable_worktree(fixture: _Fixture) -> None:
        snapshot = replace(
            fixture.worktrees.snapshots["wt-worker-0"],
            state=WorktreeState.CREATING,
        )
        fixture.worktrees.snapshots[snapshot.worktree_id.value] = snapshot

    def unavailable_checkpoint(fixture: _Fixture) -> None:
        checkpoint = replace(
            fixture.checkpoints.checkpoints["cp-worker-0"],
            state=CheckpointState.CAPTURING,
        )
        fixture.checkpoints.checkpoints[checkpoint.checkpoint_id.value] = checkpoint

    def corrupt_checkpoint(fixture: _Fixture) -> None:
        checkpoint = replace(
            fixture.checkpoints.checkpoints["cp-worker-0"],
            head_sha="b" * 40,
        )
        fixture.checkpoints.checkpoints[checkpoint.checkpoint_id.value] = checkpoint

    def wrong_parent_checkout(fixture: _Fixture) -> None:
        fixture.parent.repository = replace(
            fixture.parent.repository,
            source_worktree=tmp_path / "not-the-parent-checkout",
        )

        class _WrongCheckoutReader:
            async def inspect(self, root: Path, /) -> ParentWorkspaceSnapshot:
                del root
                return fixture.parent.snapshot()

        fixture.service._parent_reader = _WrongCheckoutReader()  # type: ignore[attr-defined]

    cases = (
        ("missing-swarm", missing_swarm, "completed Swarm run is missing", "unmanaged"),
        ("unfinished-swarm", unfinished_swarm, "Swarm run is not completed", "stale_source"),
        ("wrong-swarm-parent", wrong_swarm_parent, "Swarm parent identity", "integrity"),
        (
            "missing-swarm-dag-identity",
            missing_swarm_dag_identity,
            "completed Swarm DAG identity is incomplete",
            "integrity",
        ),
        ("missing-dag", missing_dag, "completed source DAG is missing", "unmanaged"),
        ("stale-dag", stale_dag, "source DAG identity or state is stale", "stale_source"),
        ("failed-node", failed_node, "is not completed", "stale_source"),
        ("incomplete-node", incomplete_node_linkage, "incomplete durable linkage", "integrity"),
        ("missing-lease", missing_lease, "source lease for node", "stale_source"),
        ("unfinished-lease", unfinished_lease, "source lease for node", "stale_source"),
        ("mismatched-lease", mismatched_lease, "does not match its DAG projection", "integrity"),
        (
            "unavailable-worktree",
            unavailable_worktree,
            "not the preserved managed target",
            "stale_source",
        ),
        ("unavailable-checkpoint", unavailable_checkpoint, "source baseline", "stale_source"),
        (
            "corrupt-checkpoint",
            corrupt_checkpoint,
            "failed integrity verification",
            "integrity",
        ),
        (
            "wrong-parent-checkout",
            wrong_parent_checkout,
            "not the repository source checkout",
            "identity_mismatch",
        ),
    )

    for name, configure, message, kind in cases:
        fixture = await _make_fixture(tmp_path / name)
        configure(fixture)
        with pytest.raises(ResultAdoptionError, match=message) as failure:
            await fixture.service.prepare(fixture.request)
        assert failure.value.kind == kind
        assert await fixture.store.get_result_adoption(fixture.request.adoption_id) is None
        assert fixture.mutation.calls == []


def test_result_adoption_target_operation_matches_three_way_presence() -> None:
    created = ResultAdoptionTarget(
        path="created.txt",
        operation=ResultAdoptionOperation.CREATE,
        baseline=None,
        desired=_entry("created.txt", b"created\n", scope=WorkspaceFileScope.UNTRACKED),
    )
    updated = ResultAdoptionTarget(
        path="updated.txt",
        operation=ResultAdoptionOperation.UPDATE,
        baseline=_entry("updated.txt", b"before\n"),
        desired=_entry("updated.txt", b"after\n"),
    )
    deleted = ResultAdoptionTarget(
        path="deleted.txt",
        operation=ResultAdoptionOperation.DELETE,
        baseline=_entry("deleted.txt", b"remove\n"),
        desired=None,
    )

    assert created.pre_image_fingerprint != created.desired_fingerprint
    assert updated.operation is ResultAdoptionOperation.UPDATE
    assert deleted.desired_fingerprint == workspace_entry_fingerprint(None)


def test_result_adoption_rejects_unsafe_paths_and_unsupported_worker_images() -> None:
    result_adoption_workflow._safe_target_path("src/allowed.txt")

    for path, message in (
        ("../escape.txt", "traversal"),
        (".git/config", "protected"),
        ("src/.env", "credential"),
    ):
        with pytest.raises(ResultAdoptionError, match=message):
            result_adoption_workflow._safe_target_path(path)

    symlink = WorkspaceFileEntry(
        path="link.txt",
        scope=WorkspaceFileScope.TRACKED,
        present=True,
        kind=WorkspaceFileKind.SYMLINK,
        mode=0o120000,
        link_target="outside.txt",
    )
    symlink_target = ResultAdoptionTarget(
        path="link.txt",
        operation=ResultAdoptionOperation.UPDATE,
        baseline=symlink,
        desired=replace(symlink, link_target="other.txt"),
    )
    assert ResultAdoptionTarget.from_dict(symlink_target.to_dict()) == symlink_target
    assert workspace_entry_fingerprint(symlink) != workspace_entry_fingerprint(None)

    with pytest.raises(ResultAdoptionError, match="symlink"):
        result_adoption_workflow._changed_target(
            "link.txt",
            _entry("link.txt", BASE_CONTENT_A),
            symlink,
        )
    with pytest.raises(ResultAdoptionError, match="scope changed"):
        result_adoption_workflow._changed_target(
            "scope.txt",
            _entry("scope.txt", BASE_CONTENT_A),
            _entry("scope.txt", DESIRED_CONTENT_A, scope=WorkspaceFileScope.UNTRACKED),
        )
    with pytest.raises(ResultAdoptionError, match="mode-only"):
        result_adoption_workflow._changed_target(
            "mode.txt",
            _entry("mode.txt", BASE_CONTENT_A),
            replace(_entry("mode.txt", BASE_CONTENT_A), mode=0o100755),
        )

    created = result_adoption_workflow._changed_target(
        "created.txt", None, _entry("created.txt", DESIRED_CONTENT_C)
    )
    deleted = result_adoption_workflow._changed_target(
        "deleted.txt", _entry("deleted.txt", BASE_CONTENT_A), None
    )
    assert created is not None
    assert created.operation is ResultAdoptionOperation.CREATE
    assert deleted is not None
    assert deleted.operation is ResultAdoptionOperation.DELETE

    projection = _projection(
        BASE_SHA,
        [
            _entry("duplicate.txt", BASE_CONTENT_A),
            _entry("duplicate.txt", BASE_CONTENT_A, scope=WorkspaceFileScope.UNTRACKED),
        ],
        branch=None,
        detached=True,
    )
    with pytest.raises(ResultAdoptionError, match="overlapping scopes"):
        result_adoption_workflow._projection_entries(projection)


@pytest.mark.asyncio
async def test_exact_workspace_mutation_enforces_local_exact_file_boundary(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "A.txt").write_bytes(BASE_CONTENT_A)
    tool = ExactWorkspaceMutationTool()
    update = WorkspaceMutationRequest(
        path="A.txt",
        operation=ResultAdoptionOperation.UPDATE,
        expected=_entry("A.txt", BASE_CONTENT_A),
        desired=_entry("A.txt", DESIRED_CONTENT_A),
    )
    arguments = {
        "path": update.path,
        "operation": update.operation.value,
        "_workspace_mutation_request": update,
    }
    context = ToolContext(cwd=workspace, sandbox_profile=SandboxProfile.WORKSPACE)

    assert tool.workspace_target_paths(arguments) == ("A.txt",)
    assert tool.workspace_target_paths({}) == ()
    with pytest.raises(ToolError, match="request is missing"):
        tool.prepare_filesystem_targets({}, context)
    with pytest.raises(ToolError, match="request is missing"):
        await tool.execute({}, context)
    with pytest.raises(ToolError, match="local parent"):
        await tool.execute(
            arguments,
            replace(context, client_file_system=cast(Any, object())),
        )
    with pytest.raises(ToolError, match="prohibits"):
        await tool.execute(arguments, replace(context, sandbox_profile=SandboxProfile.READ_ONLY))
    mismatched = replace(
        update,
        expected=_entry("A.txt", b"unexpected\n"),
    )
    with pytest.raises(ToolError, match="changed before"):
        await tool.execute(
            {
                "path": mismatched.path,
                "operation": mismatched.operation.value,
                "_workspace_mutation_request": mismatched,
            },
            context,
        )

    delete_with_desired = WorkspaceMutationRequest(
        path="A.txt",
        operation=ResultAdoptionOperation.DELETE,
        expected=_entry("A.txt", BASE_CONTENT_A),
        desired=_entry("A.txt", DESIRED_CONTENT_A),
    )
    with pytest.raises(ToolError, match="cannot carry"):
        await tool.execute(
            {
                "path": delete_with_desired.path,
                "operation": delete_with_desired.operation.value,
                "_workspace_mutation_request": delete_with_desired,
            },
            context,
        )

    symlink = WorkspaceFileEntry(
        path="new-link.txt",
        scope=WorkspaceFileScope.UNTRACKED,
        present=True,
        kind=WorkspaceFileKind.SYMLINK,
        mode=0o120000,
        link_target="target.txt",
    )
    with pytest.raises(ToolError, match="regular-file content"):
        await tool.execute(
            {
                "path": symlink.path,
                "operation": ResultAdoptionOperation.CREATE.value,
                "_workspace_mutation_request": WorkspaceMutationRequest(
                    path=symlink.path,
                    operation=ResultAdoptionOperation.CREATE,
                    expected=None,
                    desired=symlink,
                ),
            },
            context,
        )

    unsupported_mode = replace(_entry("mode.txt", b"mode\n"), mode=0o100600)
    with pytest.raises(ToolError, match="file mode"):
        await tool.execute(
            {
                "path": unsupported_mode.path,
                "operation": ResultAdoptionOperation.CREATE.value,
                "_workspace_mutation_request": WorkspaceMutationRequest(
                    path=unsupported_mode.path,
                    operation=ResultAdoptionOperation.CREATE,
                    expected=None,
                    desired=unsupported_mode,
                ),
            },
            context,
        )


def test_exact_workspace_image_helpers_fail_closed(tmp_path: Path) -> None:
    from neuro_code.infrastructure.tools import filesystem as filesystem_tools

    missing = tmp_path / "missing.txt"
    expected = _entry("missing.txt", BASE_CONTENT_A)
    filesystem_tools._assert_exact_regular_image(missing, None)
    with pytest.raises(ToolError, match="changed before"):
        filesystem_tools._assert_exact_regular_image(missing, expected)

    regular = tmp_path / "regular.txt"
    regular.write_bytes(BASE_CONTENT_A)
    with pytest.raises(ToolError, match="changed before"):
        filesystem_tools._assert_exact_regular_image(regular, None)
    with pytest.raises(ToolError, match="changed before"):
        filesystem_tools._assert_exact_regular_image(
            regular,
            _entry("regular.txt", b"different\n"),
        )
    with pytest.raises(ToolError, match="unsupported"):
        filesystem_tools._assert_exact_regular_image(
            regular,
            WorkspaceFileEntry(
                path="regular.txt",
                scope=WorkspaceFileScope.TRACKED,
                present=True,
                kind=WorkspaceFileKind.SYMLINK,
                mode=0o120000,
                link_target="target.txt",
            ),
        )

    link = tmp_path / "link.txt"
    link.symlink_to(regular)
    with pytest.raises(ToolError, match="link-like"):
        filesystem_tools._assert_exact_regular_image(link, expected)
    with pytest.raises(ToolError, match="mode"):
        filesystem_tools._write_exact_regular(
            regular,
            expected,
            DESIRED_CONTENT_A,
            0o100600,
        )
    with pytest.raises(ToolError, match="write failed"):
        filesystem_tools._write_exact_regular(
            tmp_path / "missing-parent" / "file.txt",
            None,
            DESIRED_CONTENT_A,
            0o100644,
        )


@pytest.mark.asyncio
async def test_retryable_target_stays_recoverable_until_expected_image_can_be_written(
    tmp_path: Path,
) -> None:
    fixture = await _make_fixture(tmp_path)
    retryable = _FailOnceMutation(fixture.parent)
    fixture.mutation = retryable
    service = fixture.new_service()

    first = await service.adopt(fixture.request)

    assert first.state is ResultAdoptionState.APPLYING
    assert first.targets[0].state is ResultAdoptionTargetState.RETRYABLE
    assert retryable.calls == []

    second = await service.adopt(fixture.request)

    assert second.state is ResultAdoptionState.COMPLETED
    assert [call.path for call in retryable.calls] == ["A.txt", "C.txt"]


@pytest.mark.asyncio
async def test_result_adoption_reconciles_mutation_failures_and_final_verification_races(
    tmp_path: Path,
) -> None:
    denied_fixture = await _make_fixture(tmp_path / "permission-denied")
    denied = _PermissionDeniedMutation()
    denied_fixture.service._mutation = denied  # type: ignore[attr-defined]

    failed = await denied_fixture.service.adopt(denied_fixture.request)

    assert failed.state is ResultAdoptionState.FAILED
    assert failed.error_kind == "permission_denied"
    assert [call.path for call in denied.calls] == ["A.txt"]
    assert denied_fixture.parent.current("A.txt") == _entry("A.txt", BASE_CONTENT_A)

    applied_fixture = await _make_fixture(tmp_path / "apply-then-fail")
    applied = _ApplyThenFailMutation(applied_fixture.parent)
    applied_fixture.service._mutation = applied  # type: ignore[attr-defined]

    applied_result = await applied_fixture.service.adopt(applied_fixture.request)

    assert applied_result.state is ResultAdoptionState.COMPLETED
    assert [call.path for call in applied.calls] == ["A.txt", "C.txt"]
    assert applied_fixture.parent.current("A.txt") == _entry("A.txt", DESIRED_CONTENT_A)

    noop_fixture = await _make_fixture(tmp_path / "noop-once")
    noop = _NoopOnceMutation(noop_fixture.parent)
    noop_fixture.service._mutation = noop  # type: ignore[attr-defined]

    first = await noop_fixture.service.adopt(noop_fixture.request)
    second = await noop_fixture.service.adopt(noop_fixture.request)

    assert first.state is ResultAdoptionState.APPLYING
    assert first.targets[0].state is ResultAdoptionTargetState.RETRYABLE
    assert second.state is ResultAdoptionState.COMPLETED
    assert [call.path for call in noop.calls] == ["A.txt", "A.txt", "C.txt"]

    inspect_fixture = await _make_fixture(tmp_path / "inspect-failure")
    inspect_reader = _FailingParentReader(inspect_fixture.parent, fail_on=3)
    inspect_fixture.service._parent_reader = inspect_reader  # type: ignore[attr-defined]
    inspect_fixture.service._mutation = _PermissionDeniedMutation()  # type: ignore[attr-defined]

    indeterminate = await inspect_fixture.service.adopt(inspect_fixture.request)

    assert indeterminate.state is ResultAdoptionState.INDETERMINATE
    assert inspect_reader.calls == 3
    assert inspect_fixture.parent.current("A.txt") == _entry("A.txt", BASE_CONTENT_A)

    race_fixture = await _make_fixture(tmp_path / "final-verification-race")
    race_reader = _FinalVerificationRaceReader(race_fixture.parent)
    race_fixture.service._parent_reader = race_reader  # type: ignore[attr-defined]

    raced = await race_fixture.service.adopt(race_fixture.request)

    assert raced.state is ResultAdoptionState.INDETERMINATE
    assert race_reader.calls == 8
    raced_target = await race_fixture.store.get_result_adoption_target(
        race_fixture.request.adoption_id,
        0,
    )
    assert raced_target is not None
    assert raced_target.state is ResultAdoptionTargetState.INDETERMINATE
    assert race_fixture.parent.current("A.txt") == _entry("A.txt", EXTERNAL_CONTENT_A)


@pytest.mark.asyncio
async def test_parent_same_path_conflict_is_durable_and_has_zero_writes(tmp_path: Path) -> None:
    fixture = await _make_fixture(tmp_path, parent_conflict=True)

    result = await fixture.service.adopt(fixture.request)

    assert result.state is ResultAdoptionState.CONFLICT
    assert result.error_kind == "conflict"
    assert fixture.mutation.calls == []
    assert fixture.parent.current("A.txt") == _entry("A.txt", EXTERNAL_CONTENT_A)
    target = await fixture.store.get_result_adoption_target(fixture.request.adoption_id, 0)
    assert target is not None
    assert target.state is ResultAdoptionTargetState.CONFLICT


@pytest.mark.asyncio
async def test_stale_worker_and_overlapping_worker_results_fail_closed_before_plan(
    tmp_path: Path,
) -> None:
    stale = await _make_fixture(tmp_path / "stale", stale_worker=True)
    with pytest.raises(ResultAdoptionError, match="changed after preservation") as stale_error:
        await stale.service.adopt(stale.request)
    assert stale_error.value.kind == "stale_source"
    assert stale.mutation.calls == []
    assert await stale.store.get_result_adoption(stale.request.adoption_id) is None

    overlap = await _make_fixture(tmp_path / "overlap", overlap=True)
    with pytest.raises(ResultAdoptionError, match="overlap") as overlap_error:
        await overlap.service.adopt(overlap.request)
    assert overlap_error.value.kind == "conflict"
    assert overlap.mutation.calls == []
    assert await overlap.store.get_result_adoption(overlap.request.adoption_id) is None


async def _mark_target_applying(fixture: _Fixture) -> None:
    record = await fixture.service.prepare(fixture.request)
    now = record.updated_at + timedelta(seconds=1)
    verified = replace(
        record,
        state=ResultAdoptionState.VERIFIED,
        updated_at=now,
        version=record.version + 1,
    )
    record = await fixture.graph.transition_result_adoption(
        verified,
        expected_version=record.version,
        expected_state=ResultAdoptionState.CLAIMED,
    )
    applying = replace(
        record,
        state=ResultAdoptionState.APPLYING,
        updated_at=now,
        version=record.version + 1,
    )
    record = await fixture.graph.transition_result_adoption(
        applying,
        expected_version=record.version,
        expected_state=ResultAdoptionState.VERIFIED,
    )
    target = await fixture.store.get_result_adoption_target(fixture.request.adoption_id, 0)
    assert target is not None
    target_applying = replace(
        target,
        state=ResultAdoptionTargetState.APPLYING,
        observed_fingerprint=target.target.pre_image_fingerprint,
        updated_at=now,
        version=target.version + 1,
    )
    await fixture.graph.transition_result_adoption_target(
        target_applying,
        adoption_id=fixture.request.adoption_id,
        ordinal=0,
        owner_pid=record.owner_pid,
        owner_token=record.owner_token,
        expected_version=target.version,
        expected_state=ResultAdoptionTargetState.NOT_STARTED,
    )


@pytest.mark.asyncio
async def test_fresh_controller_recovers_applying_target_without_rewriting_desired_image(
    tmp_path: Path,
) -> None:
    fixture = await _make_fixture(tmp_path)
    fixture.service._owner_pid = 2_147_483_647  # type: ignore[attr-defined]
    await _mark_target_applying(fixture)
    fixture.parent.set_entry(_entry("A.txt", DESIRED_CONTENT_A), path="A.txt")

    recovered = await fixture.new_service().adopt(fixture.request)

    assert recovered.state is ResultAdoptionState.COMPLETED
    assert [call.path for call in fixture.mutation.calls] == ["C.txt"]


@pytest.mark.asyncio
async def test_fresh_controller_marks_neither_image_indeterminate_without_overwrite(
    tmp_path: Path,
) -> None:
    fixture = await _make_fixture(tmp_path)
    fixture.service._owner_pid = 2_147_483_647  # type: ignore[attr-defined]
    await _mark_target_applying(fixture)
    fixture.parent.set_entry(_entry("A.txt", EXTERNAL_CONTENT_A), path="A.txt")

    recovered = await fixture.new_service().adopt(fixture.request)

    assert recovered.state is ResultAdoptionState.INDETERMINATE
    assert fixture.mutation.calls == []
    assert fixture.parent.current("A.txt") == _entry("A.txt", EXTERNAL_CONTENT_A)
    target = await fixture.store.get_result_adoption_target(fixture.request.adoption_id, 0)
    assert target is not None
    assert target.state is ResultAdoptionTargetState.INDETERMINATE


@pytest.mark.asyncio
async def test_spawned_controller_death_after_durable_plan_reuses_exact_plan_without_duplicate_writes(
    tmp_path: Path,
) -> None:
    fixture = await _make_fixture(tmp_path)
    # Model a controller that claimed the plan and then exited.  The sentinel
    # is never a live process, so the fresh controller may take the durable
    # ownership fence without touching the parent before the crash boundary.
    fixture.service._owner_pid = 2_147_483_647  # type: ignore[attr-defined]
    prepared = await fixture.service.prepare(fixture.request)
    marker = tmp_path / "plan-observed.marker"
    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=_crash_after_durable_result_adoption_plan,
        args=(str(fixture.store.database_path), fixture.request.adoption_id, str(marker)),
    )
    process.start()
    process.join(30)
    try:
        assert process.exitcode == 73
    finally:
        process.close()
    assert marker.read_text(encoding="ascii").strip() == prepared.plan_fingerprint

    recovered = await fixture.new_service().adopt(fixture.request)

    assert recovered.state is ResultAdoptionState.COMPLETED
    assert [call.path for call in fixture.mutation.calls] == ["A.txt", "C.txt"]
    assert (await fixture.store.get_result_adoption(fixture.request.adoption_id)) == recovered


class _CompositionProvider:
    provider_name = "fixture"
    model_name = "fixture-model"
    context_affinity = None
    capabilities = ModelCapabilitySet.all_unknown()

    def stream(
        self,
        context: ModelContext,
        tools: Sequence[ToolDefinition],
        *,
        tool_policy: ModelToolPolicy = ModelToolPolicy.ALLOWED,
    ) -> AsyncIterator[ModelEvent]:
        del context, tools, tool_policy

        async def empty() -> AsyncIterator[ModelEvent]:
            if False:
                yield cast(ModelEvent, object())

        return empty()


class _RealAdoptionRuntime:
    """A deterministic provider-side worker used by the production composition test."""

    def __init__(self, session_id: str, capabilities_fingerprint: str, root: Path) -> None:
        self.child_session_id = session_id
        self.capability_fingerprint = capabilities_fingerprint
        self._root = root
        self.closed = False

    async def run(self, prompt: str, *, sink=None) -> AgentRunResult:
        del sink
        if prompt == "update A":
            (self._root / "A.txt").write_bytes(DESIRED_CONTENT_A)
        elif prompt == "create C":
            (self._root / "C.txt").write_bytes(DESIRED_CONTENT_C)
        else:
            raise AssertionError(f"unexpected production worker prompt: {prompt}")
        return AgentRunResult(self.child_session_id, "worker completed", (), (), (), 1)

    async def close(self) -> None:
        self.closed = True


class _RealAdoptionRuntimeFactory(WritableSubagentRuntimeFactory):
    def __init__(self, store: SqliteSessionStore) -> None:
        self._store = store

    async def create_session(self, request: RunWritableSubagentRequest, *, capabilities) -> str:
        del request
        return await self._store.create_session(
            str(capabilities.capabilities.cwd),
            "fixture-child",
            "fixture-model",
            sandbox_profile=SandboxProfile.OFF,
        )

    async def create(
        self,
        request: RunWritableSubagentRequest,
        *,
        parent_task_id: str,
        child_session_id: str,
        capabilities,
        relay,
    ) -> _RealAdoptionRuntime:
        del parent_task_id, relay
        return _RealAdoptionRuntime(
            child_session_id,
            capabilities.fingerprint,
            capabilities.workspace_grant.canonical_child_root,
        )


class _RealAdoptionWorkerFactory:
    def __init__(self, application: ApplicationComposition, binding: ConversationBinding) -> None:
        self._application = application
        self._binding = binding

    def create(self) -> WritableSubagentApplicationService:
        return WritableSubagentApplicationService(
            self._application.store,
            cast(WritableSubagentLeaseStore, self._application.store),
            self._application.create_worktree_service(),
            self._application.create_workspace_checkpoint_service(),
            _RealAdoptionRuntimeFactory(self._application.store),
            parent_binding=self._binding,
            global_policy=self._application.subagent_global_policy(),
        )


async def _persist_completed_swarm_run(
    store: SqliteSessionStore,
    parent_session_id: str,
    dag: TaskDag,
) -> AgentSwarmRun:
    now = datetime.now(UTC)
    run_id = "swarm-real-result-adoption"
    owner_id = "real-adoption-test-owner"
    candidate = AgentSwarmRun(
        swarm_run_id=run_id,
        parent_session_id=parent_session_id,
        objective_fingerprint=objective_fingerprint("adopt completed workers"),
        planning_id="planning-real-result-adoption",
        state=AgentSwarmRunState.CLAIMED,
        generation=0,
        owner_id=owner_id,
        owner_pid=os.getpid(),
        owner_token="real-adoption-test-token",
        lease_expires_at=now + timedelta(minutes=5),
        created_at=now,
        updated_at=now,
    )
    claim = await store.claim_swarm_run(candidate, now=now, owner_is_alive=lambda _pid: True)
    assert claim.acquired
    run = claim.run

    async def advance(state: AgentSwarmRunState, **fields: object) -> None:
        nonlocal run
        expected_state = run.state
        expected_generation = run.generation
        run = replace(
            run,
            state=state,
            generation=expected_generation + 1,
            updated_at=now + timedelta(seconds=expected_generation + 1),
            **fields,
        )
        run = await store.compare_and_transition_swarm_run(
            run,
            expected_generation=expected_generation,
            expected_state=expected_state,
        )

    await advance(AgentSwarmRunState.PLANNING)
    await advance(
        AgentSwarmRunState.PLANNED,
        root_dag_id=dag.dag_id,
        current_dag_id=dag.dag_id,
        current_dag_generation=dag.generation,
        current_dag_definition_fingerprint=dag.definition_fingerprint,
    )
    await advance(AgentSwarmRunState.EXECUTING)
    response = "completed workers"
    await advance(
        AgentSwarmRunState.FINALIZING,
        final_response=response,
        final_result_fingerprint=terminal_result_fingerprint(
            run_id,
            dag.dag_id,
            dag.generation,
            dag.definition_fingerprint,
            response,
        ),
    )
    await advance(AgentSwarmRunState.COMPLETED)
    return run


def _write_composition_config(state_directory: Path) -> None:
    state_directory.mkdir(parents=True, exist_ok=True)
    (state_directory / "config.toml").write_text(
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


@pytest.mark.asyncio
async def test_real_application_composition_binds_parent_mutation_to_git_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository_path = tmp_path / "composition-parent"
    repository_path.mkdir()
    _run_git(repository_path, "init", "-q")
    _run_git(repository_path, "config", "user.email", "neuro-code-tests@example.invalid")
    _run_git(repository_path, "config", "user.name", "Neuro Code Tests")
    (repository_path / "A.txt").write_bytes(BASE_CONTENT_A)
    (repository_path / "U.txt").write_bytes(b"unrelated dirty\n")
    _run_git(repository_path, "add", "A.txt")
    _run_git(repository_path, "commit", "-qm", "initial")
    head_sha = _run_git(repository_path, "rev-parse", "HEAD").decode().strip()
    state_directory = tmp_path / "composition-state"
    _write_composition_config(state_directory)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("NEURO_CODE_HOME", str(state_directory))
    monkeypatch.setenv("FIXTURE_KEY", "fixture-key")

    application = await ApplicationComposition.open(
        ApplicationSettings(
            cwd=repository_path,
            provider="fixture",
            sandbox="off",
            permission_mode=PermissionMode.BYPASS,
            max_steps=8,
        ),
        provider_factory=lambda _config, _failover: cast(ModelProvider, _CompositionProvider()),
    )
    binding: ConversationBinding | None = None
    try:
        parent_session_id = await application.store.create_session(
            str(repository_path),
            "fixture",
            "fixture-model",
            sandbox_profile=SandboxProfile.OFF,
        )
        binding = await application.create_binding(
            resume_id=parent_session_id,
            capabilities=_capability(repository_path, sandbox=SandboxProfile.OFF),
        )
        adoption = application.create_result_adoption_service(parent_binding=binding)
        await adoption.initialize()
        assert adoption.parent_session_id == parent_session_id
        assert binding.workspace_mutation is not None

        git = LocalGitWorktreeAdapter(hooks_directory=state_directory / "git-hooks")
        reader = LocalParentWorkspaceProjectionReader(
            git=git,
            state=LocalWorkspaceStateAdapter(git=git, workspace_git=git),
        )
        before = await reader.inspect(repository_path)
        original_a = next(entry for entry in before.projection.entries if entry.path == "A.txt")
        desired_a = replace(original_a, content=DESIRED_CONTENT_A)
        await binding.workspace_mutation.apply(
            WorkspaceMutationRequest(
                path="A.txt",
                operation=ResultAdoptionOperation.UPDATE,
                expected=original_a,
                desired=desired_a,
            ),
            session_id=parent_session_id,
        )

        created_c = _entry("C.txt", DESIRED_CONTENT_C, scope=WorkspaceFileScope.UNTRACKED)
        await binding.workspace_mutation.apply(
            WorkspaceMutationRequest(
                path="C.txt",
                operation=ResultAdoptionOperation.CREATE,
                expected=None,
                desired=created_c,
            ),
            session_id=parent_session_id,
        )
        await binding.workspace_mutation.apply(
            WorkspaceMutationRequest(
                path="C.txt",
                operation=ResultAdoptionOperation.DELETE,
                expected=created_c,
                desired=None,
            ),
            session_id=parent_session_id,
        )

        assert (repository_path / "A.txt").read_bytes() == DESIRED_CONTENT_A
        assert not (repository_path / "C.txt").exists()
        assert (repository_path / "U.txt").read_bytes() == b"unrelated dirty\n"
        assert _run_git(repository_path, "rev-parse", "HEAD").decode().strip() == head_sha
        after = await reader.inspect(repository_path)
        assert (
            next(entry for entry in after.projection.entries if entry.path == "A.txt") == desired_a
        )
    finally:
        if binding is not None:
            await binding.close()
        await application.close()


@pytest.mark.asyncio
async def test_real_delete_path_requires_exact_approval_without_workspace_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository_path = tmp_path / "delete-approval-parent"
    repository_path.mkdir()
    _run_git(repository_path, "init", "-q")
    _run_git(repository_path, "config", "user.email", "neuro-code-tests@example.invalid")
    _run_git(repository_path, "config", "user.name", "Neuro Code Tests")
    (repository_path / "A.txt").write_bytes(BASE_CONTENT_A)
    (repository_path / "delete-me.txt").write_bytes(b"delete-me\n")
    _run_git(repository_path, "add", "A.txt")
    _run_git(repository_path, "commit", "-qm", "initial")
    state_directory = tmp_path / "delete-approval-state"
    _write_composition_config(state_directory)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("NEURO_CODE_HOME", str(state_directory))
    monkeypatch.setenv("FIXTURE_KEY", "fixture-key")

    class _RecordingApprover:
        def __init__(self) -> None:
            self.requests: list[Any] = []

        async def request(self, request: Any) -> PermissionApproval:
            self.requests.append(request)
            return PermissionApproval.allow_once()

    approver = _RecordingApprover()
    application = await ApplicationComposition.open(
        ApplicationSettings(
            cwd=repository_path,
            provider="fixture",
            sandbox="off",
            permission_mode=PermissionMode.DEFAULT,
            max_steps=8,
        ),
        provider_factory=lambda _config, _failover: cast(ModelProvider, _CompositionProvider()),
    )
    binding: ConversationBinding | None = None
    try:
        parent_session_id = await application.store.create_session(
            str(repository_path),
            "fixture",
            "fixture-model",
            sandbox_profile=SandboxProfile.OFF,
        )
        binding = await application.create_binding(
            approver=cast(Any, approver),
            resume_id=parent_session_id,
            capabilities=_capability(repository_path, sandbox=SandboxProfile.OFF),
        )
        assert binding.workspace_mutation is not None
        git = LocalGitWorktreeAdapter(hooks_directory=state_directory / "git-hooks")
        reader = LocalParentWorkspaceProjectionReader(
            git=git,
            state=LocalWorkspaceStateAdapter(git=git, workspace_git=git),
        )
        before = await reader.inspect(repository_path)
        delete_entry = next(
            entry for entry in before.projection.entries if entry.path == "delete-me.txt"
        )
        await binding.workspace_mutation.apply(
            WorkspaceMutationRequest(
                path="delete-me.txt",
                operation=ResultAdoptionOperation.DELETE,
                expected=delete_entry,
                desired=None,
            ),
            session_id=parent_session_id,
        )

        assert not (repository_path / "delete-me.txt").exists()
        assert len(approver.requests) == 1
        assert approver.requests[0].tool_name == "apply_patch"
        assert approver.requests[0].scope_candidates == ()
    finally:
        if binding is not None:
            await binding.close()
        await application.close()


@pytest.mark.asyncio
async def test_real_parent_mutation_honors_explicit_deny_for_result_adoption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository_path = tmp_path / "deny-parent"
    repository_path.mkdir()
    _run_git(repository_path, "init", "-q")
    _run_git(repository_path, "config", "user.email", "neuro-code-tests@example.invalid")
    _run_git(repository_path, "config", "user.name", "Neuro Code Tests")
    (repository_path / "A.txt").write_bytes(BASE_CONTENT_A)
    _run_git(repository_path, "add", "A.txt")
    _run_git(repository_path, "commit", "-qm", "initial")
    state_directory = tmp_path / "deny-state"
    _write_composition_config(state_directory)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("NEURO_CODE_HOME", str(state_directory))
    monkeypatch.setenv("FIXTURE_KEY", "fixture-key")

    application = await ApplicationComposition.open(
        ApplicationSettings(
            cwd=repository_path,
            provider="fixture",
            sandbox="off",
            permission_mode=PermissionMode.BYPASS,
            permission_rules=(PermissionRule(PermissionEffect.DENY, "apply_patch"),),
            max_steps=8,
        ),
        provider_factory=lambda _config, _failover: cast(ModelProvider, _CompositionProvider()),
    )
    binding: ConversationBinding | None = None
    try:
        parent_session_id = await application.store.create_session(
            str(repository_path),
            "fixture",
            "fixture-model",
            sandbox_profile=SandboxProfile.OFF,
        )
        binding = await application.create_binding(
            resume_id=parent_session_id,
            capabilities=_capability(repository_path, sandbox=SandboxProfile.OFF),
        )
        assert binding.workspace_mutation is not None
        git = LocalGitWorktreeAdapter(hooks_directory=state_directory / "git-hooks")
        reader = LocalParentWorkspaceProjectionReader(
            git=git,
            state=LocalWorkspaceStateAdapter(git=git, workspace_git=git),
        )
        before = await reader.inspect(repository_path)
        original_a = next(entry for entry in before.projection.entries if entry.path == "A.txt")
        desired_a = replace(original_a, content=DESIRED_CONTENT_A)

        with pytest.raises(ToolError, match="permission denied"):
            await binding.workspace_mutation.apply(
                WorkspaceMutationRequest(
                    path="A.txt",
                    operation=ResultAdoptionOperation.UPDATE,
                    expected=original_a,
                    desired=desired_a,
                ),
                session_id=parent_session_id,
            )

        assert (repository_path / "A.txt").read_bytes() == BASE_CONTENT_A
    finally:
        if binding is not None:
            await binding.close()
        await application.close()


@pytest.mark.asyncio
async def test_real_composed_writable_task_dag_and_adoption_preserve_worker_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the adoption core with real stores, worktrees, checkpoints, and DAG execution."""

    repository_path = tmp_path / "real-parent"
    repository_path.mkdir()
    _run_git(repository_path, "init", "-q")
    _run_git(repository_path, "config", "user.email", "neuro-code-tests@example.invalid")
    _run_git(repository_path, "config", "user.name", "Neuro Code Tests")
    _run_git(repository_path, "config", "core.autocrlf", "false")
    _run_git(repository_path, "config", "core.eol", "lf")
    (repository_path / "A.txt").write_bytes(BASE_CONTENT_A)
    (repository_path / "B.txt").write_bytes(BASE_CONTENT_B)
    (repository_path / "U.txt").write_bytes(b"unrelated dirty\n")
    _run_git(repository_path, "add", "A.txt", "B.txt")
    _run_git(repository_path, "commit", "-qm", "initial")
    head_sha = _run_git(repository_path, "rev-parse", "HEAD").decode().strip()
    state_directory = tmp_path / "real-state"
    _write_composition_config(state_directory)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("NEURO_CODE_HOME", str(state_directory))
    monkeypatch.setenv("FIXTURE_KEY", "fixture-key")

    application = await ApplicationComposition.open(
        ApplicationSettings(
            cwd=repository_path,
            provider="fixture",
            sandbox="off",
            permission_mode=PermissionMode.BYPASS,
            max_steps=8,
        ),
        provider_factory=lambda _config, _failover: cast(ModelProvider, _CompositionProvider()),
    )
    binding: ConversationBinding | None = None
    try:
        parent_session_id = await application.store.create_session(
            str(repository_path),
            "fixture",
            "fixture-model",
            sandbox_profile=SandboxProfile.OFF,
        )
        binding = await application.create_binding(
            resume_id=parent_session_id,
            capabilities=_capability(repository_path, sandbox=SandboxProfile.OFF),
        )
        parent_capabilities = _capability(repository_path, sandbox=SandboxProfile.OFF)
        writable = WritableSubagentApplicationService(
            application.store,
            cast(WritableSubagentLeaseStore, application.store),
            application.create_worktree_service(),
            application.create_workspace_checkpoint_service(),
            _RealAdoptionRuntimeFactory(application.store),
            parent_binding=binding,
            global_policy=application.subagent_global_policy(),
        )
        dag_service = TaskDagApplicationService(
            application.store,
            cast(TaskDagStore, application.store),
            writable,
            cast(WritableSubagentLeaseStore, application.store),
            cast(Any, application.store),
            parent_binding=binding,
            dependency_relay_store=cast(Any, application.store),
            recovery_claim_store=cast(Any, application.store),
            writable_worker_factory=_RealAdoptionWorkerFactory(application, binding),
        )
        dag = await dag_service.create_task_dag(
            CreateTaskDagRequest(
                "dag-real-result-adoption",
                (
                    TaskDagNode(node_id="node-update-a", ordinal=0, prompt="update A"),
                    TaskDagNode(node_id="node-create-c", ordinal=1, prompt="create C"),
                ),
                max_parallel=2,
            )
        )
        dag = await dag_service.run_task_dag(RunTaskDagRequest(dag.dag_id))
        assert dag.state is TaskDagState.COMPLETED
        assert all(node.state is TaskDagNodeState.COMPLETED for node in dag.nodes)
        assert all(
            node.lease_id and node.worktree_id and node.baseline_checkpoint_id for node in dag.nodes
        )

        swarm = await _persist_completed_swarm_run(application.store, parent_session_id, dag)

        # A: a real fresh composition persists the plan and dies before any
        # parent mutation; another fresh composition claims and completes it.
        a_marker = tmp_path / "real-adoption-plan-a.marker"
        context = multiprocessing.get_context("spawn")
        a_process = context.Process(
            target=_prepare_real_result_adoption_plan_and_exit,
            args=(
                str(state_directory),
                str(repository_path),
                parent_session_id,
                swarm.swarm_run_id,
                "adopt-real-composed-a",
                str(a_marker),
            ),
        )
        a_process.start()
        a_process.join(60)
        try:
            if a_process.is_alive():
                a_process.terminate()
                a_process.join(30)
            assert a_process.exitcode == 73
        finally:
            a_process.close()
        a_plan_fingerprint = a_marker.read_text(encoding="ascii").strip()
        assert a_plan_fingerprint
        assert not (repository_path / "C.txt").exists()
        assert (repository_path / "A.txt").read_bytes() == BASE_CONTENT_A

        a_recovery_marker = tmp_path / "real-adoption-recovery-a.marker"
        a_recovery_process = context.Process(
            target=_reenter_result_adoption_in_fresh_process,
            args=(
                str(state_directory),
                str(repository_path),
                parent_session_id,
                swarm.swarm_run_id,
                "adopt-real-composed-a",
                str(a_recovery_marker),
            ),
        )
        a_recovery_process.start()
        a_recovery_process.join(60)
        try:
            if a_recovery_process.is_alive():
                a_recovery_process.terminate()
                a_recovery_process.join(30)
            assert a_recovery_process.exitcode == 0
        finally:
            a_recovery_process.close()
        a_payload = json.loads(a_recovery_marker.read_text(encoding="ascii"))
        assert a_payload["adoption_id"] == "adopt-real-composed-a"
        assert a_payload["plan_fingerprint"] == a_plan_fingerprint
        assert a_payload["state"] == ResultAdoptionState.COMPLETED.value
        assert a_payload["target_states"] == [
            ResultAdoptionTargetState.APPLIED.value,
            ResultAdoptionTargetState.APPLIED.value,
        ]
        assert a_payload["call_paths"] == ["A.txt", "C.txt"]
        a_record = await application.store.get_result_adoption("adopt-real-composed-a")
        assert a_record is not None
        assert a_record.state is ResultAdoptionState.COMPLETED
        assert a_record.plan_fingerprint == a_plan_fingerprint
        assert (repository_path / "A.txt").read_bytes() == DESIRED_CONTENT_A
        assert (repository_path / "C.txt").read_bytes() == DESIRED_CONTENT_C

        # Restore the baseline before the independent partial-application B
        # proof so each recovery scenario has its own exact pre-image.
        _write_durable_bytes(repository_path / "A.txt", BASE_CONTENT_A)
        (repository_path / "C.txt").unlink()

        # B: a fresh process applies A, dies before its durable ACK, and a
        # fresh controller acknowledges the desired image before applying C.
        marker = tmp_path / "real-adoption-crash-b.marker"
        process = context.Process(
            target=_crash_after_first_result_adoption_mutation,
            args=(
                str(state_directory),
                str(repository_path),
                parent_session_id,
                swarm.swarm_run_id,
                "adopt-real-composed",
                str(marker),
            ),
        )
        process.start()
        process.join(60)
        try:
            if process.is_alive():
                process.terminate()
                process.join(30)
            assert process.exitcode == 74
        finally:
            process.close()
        assert marker.read_text(encoding="ascii").strip() == "A.txt"

        recovery_mutation = _DelegatingRecordingMutation(binding.workspace_mutation)
        recovery_binding = replace(binding, workspace_mutation=recovery_mutation)
        adoption = application.create_result_adoption_service(parent_binding=recovery_binding)
        result = await adoption.adopt(
            ResultAdoptionRequest("adopt-real-composed", swarm.swarm_run_id)
        )

        assert result.state is ResultAdoptionState.COMPLETED
        assert [call.path for call in recovery_mutation.calls] == ["C.txt"]
        assert (repository_path / "A.txt").read_bytes() == DESIRED_CONTENT_A
        assert (repository_path / "B.txt").read_bytes() == BASE_CONTENT_B
        assert (repository_path / "C.txt").read_bytes() == DESIRED_CONTENT_C
        assert (repository_path / "U.txt").read_bytes() == b"unrelated dirty\n"
        assert _run_git(repository_path, "rev-parse", "HEAD").decode().strip() == head_sha
        assert binding.workspace_root == parent_capabilities.cwd

        # C: a fresh process reaches durable APPLYING, an unrelated actor
        # changes the target, and the process dies before the mutation ACK.
        _write_durable_bytes(repository_path / "A.txt", BASE_CONTENT_A)
        (repository_path / "C.txt").unlink()
        c_marker = tmp_path / "real-adoption-crash-c.marker"
        c_process = context.Process(
            target=_crash_after_external_result_adoption_mutation,
            args=(
                str(state_directory),
                str(repository_path),
                parent_session_id,
                swarm.swarm_run_id,
                "adopt-real-composed-c",
                str(c_marker),
            ),
        )
        c_process.start()
        c_process.join(60)
        try:
            if c_process.is_alive():
                c_process.terminate()
                c_process.join(30)
            assert c_process.exitcode == 75
        finally:
            c_process.close()
        assert c_marker.read_text(encoding="ascii").strip() == "A.txt"
        c_l1 = await application.store.get_result_adoption("adopt-real-composed-c")
        assert c_l1 is not None
        assert c_l1.state is ResultAdoptionState.APPLYING
        assert [target.state for target in c_l1.targets] == [
            ResultAdoptionTargetState.APPLYING,
            ResultAdoptionTargetState.NOT_STARTED,
        ]
        c_counts_before_l2 = _database_row_counts(state_directory)

        c_recovery_marker = tmp_path / "real-adoption-recovery-c.marker"
        c_recovery_process = context.Process(
            target=_reenter_result_adoption_in_fresh_process,
            args=(
                str(state_directory),
                str(repository_path),
                parent_session_id,
                swarm.swarm_run_id,
                "adopt-real-composed-c",
                str(c_recovery_marker),
            ),
        )
        c_recovery_process.start()
        c_recovery_process.join(60)
        try:
            if c_recovery_process.is_alive():
                c_recovery_process.terminate()
                c_recovery_process.join(30)
            assert c_recovery_process.exitcode == 0
        finally:
            c_recovery_process.close()
        c_payload = json.loads(c_recovery_marker.read_text(encoding="ascii"))
        assert c_payload["adoption_id"] == "adopt-real-composed-c"
        assert c_payload["plan_fingerprint"] == c_l1.plan_fingerprint
        assert c_payload["state"] == ResultAdoptionState.INDETERMINATE.value
        assert c_payload["target_states"] == [
            ResultAdoptionTargetState.INDETERMINATE.value,
            ResultAdoptionTargetState.NOT_STARTED.value,
        ]
        assert c_payload["call_paths"] == []
        c_after_l2 = await application.store.get_result_adoption("adopt-real-composed-c")
        assert c_after_l2 is not None
        assert c_after_l2.plan == c_l1.plan
        assert c_after_l2.state is ResultAdoptionState.INDETERMINATE
        assert c_after_l2.targets[0].state is ResultAdoptionTargetState.INDETERMINATE
        assert c_after_l2.targets[0].observed_fingerprint == workspace_entry_fingerprint(
            _entry("A.txt", EXTERNAL_CONTENT_A)
        )
        assert c_after_l2.targets[1].state is ResultAdoptionTargetState.NOT_STARTED
        assert _database_row_counts(state_directory) == c_counts_before_l2
        assert (repository_path / "A.txt").read_bytes() == EXTERNAL_CONTENT_A
        assert not (repository_path / "C.txt").exists()

        # D: complete a different real adoption in one fresh OS process, then
        # re-enter it from another fresh process and prove the terminal result
        # is a durable no-op with no new orchestration records.
        _write_durable_bytes(repository_path / "A.txt", BASE_CONTENT_A)
        (repository_path / "C.txt").unlink(missing_ok=True)
        d_marker = tmp_path / "real-adoption-complete-d.marker"
        d_process = context.Process(
            target=_complete_result_adoption_in_fresh_process,
            args=(
                str(state_directory),
                str(repository_path),
                parent_session_id,
                swarm.swarm_run_id,
                "adopt-real-composed-d",
                str(d_marker),
            ),
        )
        d_process.start()
        d_process.join(60)
        try:
            if d_process.is_alive():
                d_process.terminate()
                d_process.join(30)
            assert d_process.exitcode == 0
        finally:
            d_process.close()
        d_payload = json.loads(d_marker.read_text(encoding="ascii"))
        assert d_payload["adoption_id"] == "adopt-real-composed-d"
        assert d_payload["state"] == ResultAdoptionState.COMPLETED.value
        assert d_payload["call_paths"] == []
        d_before_l2 = await application.store.get_result_adoption("adopt-real-composed-d")
        assert d_before_l2 is not None
        assert d_before_l2.state is ResultAdoptionState.COMPLETED
        assert all(
            target.state is ResultAdoptionTargetState.APPLIED for target in d_before_l2.targets
        )
        d_counts_before_l2 = _database_row_counts(state_directory)

        d_recovery_marker = tmp_path / "real-adoption-recovery-d.marker"
        d_recovery_process = context.Process(
            target=_reenter_result_adoption_in_fresh_process,
            args=(
                str(state_directory),
                str(repository_path),
                parent_session_id,
                swarm.swarm_run_id,
                "adopt-real-composed-d",
                str(d_recovery_marker),
            ),
        )
        d_recovery_process.start()
        d_recovery_process.join(60)
        try:
            if d_recovery_process.is_alive():
                d_recovery_process.terminate()
                d_recovery_process.join(30)
            assert d_recovery_process.exitcode == 0
        finally:
            d_recovery_process.close()
        d_payload_recovered = json.loads(d_recovery_marker.read_text(encoding="ascii"))
        assert d_payload_recovered["adoption_id"] == "adopt-real-composed-d"
        assert d_payload_recovered["plan_fingerprint"] == d_before_l2.plan_fingerprint
        assert d_payload_recovered["state"] == ResultAdoptionState.COMPLETED.value
        assert d_payload_recovered["target_states"] == [
            ResultAdoptionTargetState.APPLIED.value,
            ResultAdoptionTargetState.APPLIED.value,
        ]
        assert d_payload_recovered["call_paths"] == []
        d_after_l2 = await application.store.get_result_adoption("adopt-real-composed-d")
        assert d_after_l2 == d_before_l2
        assert _database_row_counts(state_directory) == d_counts_before_l2
        assert (repository_path / "A.txt").read_bytes() == DESIRED_CONTENT_A
        assert (repository_path / "C.txt").read_bytes() == DESIRED_CONTENT_C

        worktrees = application.create_worktree_service()
        checkpoints = application.create_workspace_checkpoint_service()
        await worktrees.initialize()
        await checkpoints.initialize()
        for node in dag.nodes:
            assert node.lease_id is not None
            assert node.worktree_id is not None
            assert node.baseline_checkpoint_id is not None
            lease = await application.store.get_writable_subagent_lease(node.lease_id)
            assert lease is not None
            assert lease.state is WritableSubagentWorkspaceState.PRESERVED
            snapshot = await worktrees.inspect(node.worktree_id)
            assert snapshot.state is WorktreeState.READY
            checkpoint = await checkpoints.get(CheckpointId(node.baseline_checkpoint_id))
            assert checkpoint is not None
            assert checkpoint.state is CheckpointState.READY
            assert checkpoint.artifact_path.exists()
    finally:
        if binding is not None:
            await binding.close()
        await application.close()
