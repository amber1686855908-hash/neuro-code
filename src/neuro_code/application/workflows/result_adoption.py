"""Bounded durable adoption of preserved writable-worker results.

This workflow consumes only durable Swarm/DAG/lease/checkpoint projections and
the live managed worker projection.  It never interprets a worker response or
performs a raw filesystem write.  Parent mutations are delegated to the
runtime-owned permission/workspace/sandbox port one target at a time.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from neuro_code.application.ports.agent_swarm import AgentSwarmStore
from neuro_code.application.ports.checkpoints import (
    WorkspaceCheckpointApplication,
    WorkspaceCheckpointError,
)
from neuro_code.application.ports.result_adoption import (
    ParentWorkspaceProjectionReader,
    ParentWorkspaceSnapshot,
    ResultAdoptionError,
    ResultAdoptionRecord,
    ResultAdoptionStore,
    ResultAdoptionTargetRecord,
    ResultAdoptionWorktreePort,
    WorkspaceMutationPort,
    WorkspaceMutationRequest,
)
from neuro_code.application.ports.task_dag import TaskDagStore
from neuro_code.application.ports.worktree import WorktreeError
from neuro_code.application.ports.writable_subagent import WritableSubagentLeaseStore
from neuro_code.application.runtime.process_liveness import owner_is_alive
from neuro_code.application.workflows.subagent_capabilities import (
    WRITABLE_SUBAGENT_WRITE_TOOL_NAMES,
    SubagentCapabilitySet,
)
from neuro_code.domain.agent_swarm import AgentSwarmResult, AgentSwarmRunState
from neuro_code.domain.checkpoints import (
    CheckpointState,
    WorkspaceFileEntry,
    WorkspaceFileKind,
    WorkspaceProjection,
    workspace_projection_fingerprint,
)
from neuro_code.domain.result_adoption import (
    MAX_RESULT_ADOPTION_LEASE_SECONDS,
    MAX_RESULT_ADOPTION_PATH_BYTES,
    MAX_RESULT_ADOPTION_SOURCES,
    MAX_RESULT_ADOPTION_TARGETS,
    ResultAdoptionOperation,
    ResultAdoptionPlan,
    ResultAdoptionRequest,
    ResultAdoptionSource,
    ResultAdoptionState,
    ResultAdoptionTarget,
    ResultAdoptionTargetState,
    workspace_entry_fingerprint,
)
from neuro_code.domain.task_dag import TaskDagNodeKind, TaskDagNodeState, TaskDagState
from neuro_code.domain.worktree import WorktreeOwnership, WorktreeState
from neuro_code.domain.writable_subagent import WritableSubagentWorkspaceState

Clock = Callable[[], datetime]

if TYPE_CHECKING:
    from neuro_code.application.sessions.binding import ConversationBinding


def _now() -> datetime:
    return datetime.now(UTC)


def _present_entry(entry: WorkspaceFileEntry | None) -> WorkspaceFileEntry | None:
    if entry is None or not entry.present:
        return None
    return entry


def _projection_entries(projection: WorkspaceProjection) -> dict[str, WorkspaceFileEntry]:
    result: dict[str, WorkspaceFileEntry] = {}
    for entry in projection.entries:
        existing = result.get(entry.path)
        if existing is not None and existing.scope is not entry.scope:
            raise ResultAdoptionError(
                f"workspace projection contains overlapping scopes for {entry.path!r}",
                kind="integrity",
            )
        result[entry.path] = entry
    return result


def _entry_at(projection: WorkspaceProjection, path: str) -> WorkspaceFileEntry | None:
    return _present_entry(_projection_entries(projection).get(path))


def _entry_equal(
    first: WorkspaceFileEntry | None,
    second: WorkspaceFileEntry | None,
) -> bool:
    return first == second


def _safe_target_path(path: str) -> None:
    if (
        not isinstance(path, str)
        or not path
        or "\x00" in path
        or "\\" in path
        or path.startswith("/")
        or len(path.encode("utf-8")) > MAX_RESULT_ADOPTION_PATH_BYTES
    ):
        raise ResultAdoptionError("worker result contains an unsafe path", kind="unsafe_path")
    parts = PurePosixPath(path).parts
    if not parts or any(part in {"", ".", ".."} for part in parts) or ":" in parts[0]:
        raise ResultAdoptionError("worker result contains a traversal path", kind="unsafe_path")
    lowered = tuple(part.casefold() for part in parts)
    protected_components = frozenset(
        {
            ".git",
            ".neuro",
            ".neuro-code",
            "neuro-code-state",
            "worktrees",
            "checkpoints",
            "credentials",
            "credential",
            "secrets",
            "secret",
            "keys",
            "key",
        }
    )
    if any(part in protected_components for part in lowered):
        raise ResultAdoptionError("worker result targets a protected path", kind="unsafe_path")
    filename = lowered[-1]
    if (
        filename in {".env", ".env.local", ".env.production", "id_rsa", "id_ed25519"}
        or filename.endswith((".pem", ".key", ".p12", ".pfx", ".jks"))
        or any(token in filename for token in ("secret", "credential", "private_key"))
    ):
        raise ResultAdoptionError("worker result targets a credential path", kind="unsafe_path")


def _changed_target(
    path: str,
    baseline: WorkspaceFileEntry | None,
    desired: WorkspaceFileEntry | None,
) -> ResultAdoptionTarget | None:
    baseline = _present_entry(baseline)
    desired = _present_entry(desired)
    if _entry_equal(baseline, desired):
        return None
    _safe_target_path(path)
    for entry in (baseline, desired):
        if entry is not None and entry.kind is not WorkspaceFileKind.REGULAR:
            raise ResultAdoptionError(
                f"symlink or non-regular result target is unsupported: {path!r}",
                kind="unsupported_workspace_state",
            )
    if baseline is not None and desired is not None:
        if baseline.scope is not desired.scope:
            raise ResultAdoptionError(
                f"result target scope changed for {path!r}", kind="unsupported_workspace_state"
            )
        if baseline.content == desired.content and baseline.mode != desired.mode:
            raise ResultAdoptionError(
                f"mode-only result changes are unsupported for {path!r}",
                kind="unsupported_workspace_state",
            )
        operation = "update"
    elif baseline is None and desired is not None:
        operation = "create"
    elif baseline is not None and desired is None:
        operation = "delete"
    else:
        raise ResultAdoptionError("result target image is invalid", kind="integrity")
    return ResultAdoptionTarget(
        path=path,
        operation=ResultAdoptionOperation(operation),
        baseline=baseline,
        desired=desired,
    )


class ResultAdoptionApplicationService:
    """Prepare and forward-recover one durable parent adoption plan."""

    def __init__(
        self,
        *,
        store: ResultAdoptionStore,
        swarms: AgentSwarmStore,
        dags: TaskDagStore,
        leases: WritableSubagentLeaseStore,
        worktrees: ResultAdoptionWorktreePort,
        checkpoints: WorkspaceCheckpointApplication,
        parent_reader: ParentWorkspaceProjectionReader,
        mutation: WorkspaceMutationPort,
        parent_binding: ConversationBinding,
        clock: Clock = _now,
        lease_seconds: float = MAX_RESULT_ADOPTION_LEASE_SECONDS,
    ) -> None:
        from neuro_code.application.sessions.binding import ConversationBinding

        if not isinstance(parent_binding, ConversationBinding):
            raise ResultAdoptionError(
                "result adoption parent binding is required", kind="configuration"
            )
        session_id = parent_binding.runner.session_id
        if not isinstance(session_id, str) or not session_id.strip() or "\x00" in session_id:
            raise ResultAdoptionError(
                "result adoption parent session is unavailable", kind="configuration"
            )
        root = parent_binding.workspace_root
        if not isinstance(root, Path):
            raise ResultAdoptionError(
                "result adoption parent workspace root is unavailable", kind="configuration"
            )
        try:
            root = root.expanduser().resolve(strict=False)
        except (OSError, RuntimeError) as error:
            raise ResultAdoptionError(
                "result adoption parent workspace is unavailable", kind="configuration"
            ) from error
        if not root.is_absolute():
            raise ResultAdoptionError(
                "result adoption parent workspace root is not absolute", kind="configuration"
            )
        capabilities = parent_binding.capabilities
        if not isinstance(capabilities, SubagentCapabilitySet):
            raise ResultAdoptionError(
                "result adoption parent binding capability metadata is missing",
                kind="configuration",
            )
        if (
            not capabilities.filesystem_write
            or not capabilities.sandbox_profile.workspace_writable
            or not WRITABLE_SUBAGENT_WRITE_TOOL_NAMES.issubset(capabilities.allowed_tool_names)
            or capabilities.cwd != root
        ):
            raise ResultAdoptionError(
                "result adoption parent binding does not carry writable authority",
                kind="permission_denied",
            )
        if not callable(getattr(mutation, "apply", None)):
            raise ResultAdoptionError(
                "result adoption mutation port is unavailable", kind="configuration"
            )
        if not callable(getattr(parent_reader, "inspect", None)):
            raise ResultAdoptionError(
                "result adoption parent reader is unavailable", kind="configuration"
            )
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, (int, float)):
            raise ValueError("result adoption lease duration is invalid")
        if not 0 < float(lease_seconds) <= MAX_RESULT_ADOPTION_LEASE_SECONDS:
            raise ValueError("result adoption lease duration is out of bounds")
        self._store = store
        self._swarms = swarms
        self._dags = dags
        self._leases = leases
        self._worktrees = worktrees
        self._checkpoints = checkpoints
        self._parent_reader = parent_reader
        self._mutation = mutation
        self._parent_binding = parent_binding
        self._parent_session_id = session_id
        self._parent_root = root
        self._parent_capability_fingerprint = capabilities.fingerprint
        self._clock = clock
        self._lease_seconds = float(lease_seconds)
        self._owner_pid = os.getpid()
        self._owner_token = f"adoption-owner-{uuid.uuid4().hex}"
        self._lock = asyncio.Lock()
        self._initialized = False

    @property
    def parent_session_id(self) -> str:
        return self._parent_session_id

    async def initialize(self) -> None:
        if self._initialized:
            return
        await self._store.initialize()
        await self._worktrees.initialize()
        await self._checkpoints.initialize()
        self._initialized = True

    async def get_result_adoption(self, adoption_id: str) -> ResultAdoptionRecord | None:
        """Read one durable adoption projection without creating or claiming it."""

        await self.initialize()
        if not isinstance(adoption_id, str) or not adoption_id.strip() or "\x00" in adoption_id:
            raise ResultAdoptionError("adoption identity is invalid", kind="integrity")
        return await self._store.get_result_adoption(adoption_id)

    async def prepare(
        self,
        request: ResultAdoptionRequest,
        *,
        swarm_result: AgentSwarmResult | None = None,
    ) -> ResultAdoptionRecord:
        await self.initialize()
        if not isinstance(request, ResultAdoptionRequest):
            raise TypeError("result adoption request must be canonical")
        self._validate_swarm_result(request, swarm_result)
        existing = await self._store.get_result_adoption(request.adoption_id)
        if existing is not None:
            self._assert_existing_request(existing, request)
            return existing
        plan = await self._build_plan(request)
        now = self._clock().astimezone(UTC)
        return await self._store.insert_result_adoption(
            plan,
            owner_pid=self._owner_pid,
            owner_token=self._owner_token,
            now=now,
            lease_expires_at=now + timedelta(seconds=self._lease_seconds),
        )

    async def adopt(
        self,
        request: ResultAdoptionRequest,
        *,
        swarm_result: AgentSwarmResult | None = None,
    ) -> ResultAdoptionRecord:
        """Adopt or recover one exact durable plan; never merge arbitrary text."""

        await self.initialize()
        async with self._lock:
            record = await self.prepare(request, swarm_result=swarm_result)
            if record.state.terminal:
                return record
            now = self._clock().astimezone(UTC)
            record = await self._store.claim_result_adoption(
                record.adoption_id,
                owner_pid=self._owner_pid,
                owner_token=self._owner_token,
                now=now,
                lease_expires_at=now + timedelta(seconds=self._lease_seconds),
                owner_is_alive=owner_is_alive,
            )
            if record.state.terminal:
                return record
            if (record.owner_pid, record.owner_token) != (
                self._owner_pid,
                self._owner_token,
            ):
                raise ResultAdoptionError(
                    "another live controller owns this result adoption",
                    kind="busy",
                )
            if record.state is ResultAdoptionState.CLAIMED:
                try:
                    await self._verify_parent(record.plan, allow_desired=False)
                except ResultAdoptionError as error:
                    if error.kind == "conflict":
                        await self._mark_initial_conflicts(record)
                    return await self._terminate(record, self._conflict_state(error), error)
                record = await self._transition_adoption(record, ResultAdoptionState.VERIFIED)
            if record.state is ResultAdoptionState.VERIFIED:
                record = await self._transition_adoption(record, ResultAdoptionState.APPLYING)
            if record.state is ResultAdoptionState.APPLYING:
                record = await self._apply_targets(record)
                if record.state.terminal:
                    return record
            if record.state is ResultAdoptionState.VERIFYING:
                try:
                    return await self._complete_verification(record)
                except ResultAdoptionError as error:
                    return await self._terminate(record, ResultAdoptionState.INDETERMINATE, error)
            return record

    @staticmethod
    def _validate_swarm_result(
        request: ResultAdoptionRequest,
        swarm_result: AgentSwarmResult | None,
    ) -> None:
        if swarm_result is None:
            return
        if not isinstance(swarm_result, AgentSwarmResult):
            raise ResultAdoptionError(
                "result adoption requires the canonical Swarm result",
                kind="integrity",
            )
        if swarm_result.swarm_run_id != request.swarm_run_id:
            raise ResultAdoptionError(
                "result adoption Swarm identity does not match the request",
                kind="integrity",
            )

    def _assert_existing_request(
        self,
        record: ResultAdoptionRecord,
        request: ResultAdoptionRequest,
    ) -> None:
        if (
            record.plan.adoption_id != request.adoption_id
            or record.plan.swarm_run_id != request.swarm_run_id
            or record.plan.parent_session_id != self._parent_session_id
            or record.plan.parent_workspace_root != self._parent_root
        ):
            raise ResultAdoptionError(
                "adoption identity is bound to a different parent or swarm",
                kind="integrity",
            )

    async def _build_plan(self, request: ResultAdoptionRequest) -> ResultAdoptionPlan:
        run = await self._swarms.get_swarm_run(request.swarm_run_id)
        if run is None:
            raise ResultAdoptionError("completed Swarm run is missing", kind="unmanaged")
        if run.state is not AgentSwarmRunState.COMPLETED:
            raise ResultAdoptionError("Swarm run is not completed", kind="stale_source")
        if run.parent_session_id != self._parent_session_id or run.current_dag_id is None:
            raise ResultAdoptionError("Swarm parent identity does not match", kind="integrity")
        if run.current_dag_generation is None or run.current_dag_definition_fingerprint is None:
            raise ResultAdoptionError(
                "completed Swarm DAG identity is incomplete", kind="integrity"
            )
        dag = await self._dags.get_task_dag(run.current_dag_id)
        if dag is None:
            raise ResultAdoptionError("completed source DAG is missing", kind="unmanaged")
        if (
            dag.state is not TaskDagState.COMPLETED
            or dag.parent_session_id != self._parent_session_id
            or dag.generation != run.current_dag_generation
            or dag.definition_fingerprint != run.current_dag_definition_fingerprint
        ):
            raise ResultAdoptionError("source DAG identity or state is stale", kind="stale_source")
        if not 1 <= len(dag.nodes) <= MAX_RESULT_ADOPTION_SOURCES:
            raise ResultAdoptionError(
                "source worker count exceeds the adoption bound", kind="bounds"
            )
        parent = await self._inspect_parent()
        if parent.repository.source_worktree != self._parent_root:
            raise ResultAdoptionError(
                "parent binding root is not the repository source checkout",
                kind="identity_mismatch",
            )
        source_values: list[ResultAdoptionSource] = []
        source_targets: list[tuple[str, ResultAdoptionTarget]] = []
        seen_paths: set[str] = set()
        for node in sorted(dag.nodes, key=lambda item: (item.ordinal, item.node_id)):
            if (
                node.state is not TaskDagNodeState.COMPLETED
                or node.kind is not TaskDagNodeKind.WRITABLE_SUBAGENT
            ):
                raise ResultAdoptionError(
                    f"source node {node.node_id!r} is not completed", kind="stale_source"
                )
            if (
                not isinstance(node.parent_task_id, str)
                or not node.parent_task_id
                or not isinstance(node.child_session_id, str)
                or not node.child_session_id
                or not isinstance(node.lease_id, str)
                or not node.lease_id
                or not isinstance(node.worktree_id, str)
                or not node.worktree_id
                or not isinstance(node.baseline_checkpoint_id, str)
                or not node.baseline_checkpoint_id
                or not isinstance(node.final_workspace_fingerprint, str)
                or not node.final_workspace_fingerprint
            ):
                raise ResultAdoptionError(
                    f"source node {node.node_id!r} has incomplete durable linkage",
                    kind="integrity",
                )
            parent_task_id = node.parent_task_id
            child_session_id = node.child_session_id
            lease_id = node.lease_id
            worktree_id = node.worktree_id
            baseline_checkpoint_id = node.baseline_checkpoint_id
            final_workspace_fingerprint = node.final_workspace_fingerprint
            lease = await self._leases.get_writable_subagent_lease(lease_id)
            if lease is None or lease.state is not WritableSubagentWorkspaceState.PRESERVED:
                raise ResultAdoptionError(
                    f"source lease for node {node.node_id!r} is not preserved",
                    kind="stale_source",
                )
            if (
                lease.parent_session_id != self._parent_session_id
                or lease.parent_task_id != parent_task_id
                or lease.child_session_id != child_session_id
                or lease.worktree_id.value != worktree_id
                or lease.baseline_checkpoint_id is None
                or lease.baseline_checkpoint_id.value != baseline_checkpoint_id
                or lease.final_workspace_fingerprint != final_workspace_fingerprint
                or lease.parent_workspace_root != self._parent_root
                or lease.parent_capability_fingerprint != self._parent_capability_fingerprint
                or lease.final_workspace_fingerprint is None
                or lease.capability_fingerprint is None
                or lease.grant_fingerprint is None
            ):
                raise ResultAdoptionError(
                    f"source lease for node {node.node_id!r} does not match its DAG projection",
                    kind="integrity",
                )
            if (
                not isinstance(lease.child_session_id, str)
                or not lease.child_session_id
                or lease.final_workspace_fingerprint is None
            ):
                raise ResultAdoptionError(
                    f"source lease for node {node.node_id!r} has incomplete terminal identity",
                    kind="integrity",
                )
            if lease.parent_repository != parent.repository:
                raise ResultAdoptionError(
                    f"source lease repository for node {node.node_id!r} is stale",
                    kind="stale_source",
                )
            try:
                snapshot = await self._worktrees.inspect(lease.worktree_id.value)
            except (WorktreeError, ValueError) as error:
                raise ResultAdoptionError(
                    f"source worktree for node {node.node_id!r} is unavailable",
                    kind="stale_source",
                ) from error
            if (
                snapshot.ownership is not WorktreeOwnership.MANAGED
                or not snapshot.managed
                or snapshot.state is not WorktreeState.READY
                or snapshot.repository != parent.repository
                or snapshot.base_commit_sha != lease.base_commit_sha
                or snapshot.canonical_path != lease.canonical_child_root
                or lease.worktree is None
                or snapshot.handle != lease.worktree
            ):
                raise ResultAdoptionError(
                    f"source worktree for node {node.node_id!r} is not the preserved managed target",
                    kind="stale_source",
                )
            checkpoint = await self._checkpoints.get(lease.baseline_checkpoint_id)
            if checkpoint is None or checkpoint.state is not CheckpointState.READY:
                raise ResultAdoptionError(
                    f"source baseline for node {node.node_id!r} is not READY",
                    kind="stale_source",
                )
            if (
                checkpoint.worktree_id != snapshot.worktree_id
                or checkpoint.repository != parent.repository
                or checkpoint.canonical_path != snapshot.canonical_path
                or checkpoint.head_sha != lease.base_commit_sha
                or checkpoint.source_fingerprint.value
                != workspace_projection_fingerprint(
                    snapshot.handle,
                    await self._checkpoints.load_projection(checkpoint.checkpoint_id),
                ).value
            ):
                raise ResultAdoptionError(
                    f"source baseline for node {node.node_id!r} failed integrity verification",
                    kind="integrity",
                )
            baseline = await self._checkpoints.load_projection(checkpoint.checkpoint_id)
            live = await self._checkpoints.inspect(snapshot.handle)
            live_fingerprint = workspace_projection_fingerprint(snapshot.handle, live).value
            if live_fingerprint != lease.final_workspace_fingerprint:
                raise ResultAdoptionError(
                    f"source worker {node.node_id!r} changed after preservation",
                    kind="stale_source",
                )
            source = ResultAdoptionSource(
                node_id=node.node_id,
                parent_task_id=node.parent_task_id,
                child_session_id=lease.child_session_id,
                lease_id=lease.lease_id,
                worktree_id=lease.worktree_id,
                baseline_checkpoint_id=lease.baseline_checkpoint_id,
                base_commit_sha=lease.base_commit_sha,
                final_workspace_fingerprint=lease.final_workspace_fingerprint,
                capability_fingerprint=lease.capability_fingerprint,
                grant_fingerprint=lease.grant_fingerprint,
                parent_repository=parent.repository,
            )
            source_values.append(source)
            baseline_entries = _projection_entries(baseline)
            desired_entries = _projection_entries(live)
            for path in sorted(set(baseline_entries) | set(desired_entries)):
                target = _changed_target(
                    path,
                    baseline_entries.get(path),
                    desired_entries.get(path),
                )
                if target is None:
                    continue
                if path in seen_paths:
                    raise ResultAdoptionError(
                        f"worker results overlap on path {path!r}", kind="conflict"
                    )
                seen_paths.add(path)
                source_targets.append((node.node_id, target))
        targets = tuple(
            target
            for _node_id, target in sorted(
                source_targets,
                key=lambda item: item[1].path,
            )
        )
        if len(targets) > MAX_RESULT_ADOPTION_TARGETS:
            raise ResultAdoptionError("adoption target count exceeds its bound", kind="bounds")
        for source in source_values:
            if source.base_commit_sha != parent.repository.head_sha:
                raise ResultAdoptionError(
                    "parent HEAD no longer matches the worker base commit",
                    kind="stale_source",
                )
        return ResultAdoptionPlan(
            adoption_id=request.adoption_id,
            parent_session_id=self._parent_session_id,
            parent_workspace_root=self._parent_root,
            parent_repository=parent.repository,
            parent_head_sha=parent.repository.head_sha,
            swarm_run_id=run.swarm_run_id,
            dag_id=dag.dag_id,
            dag_generation=dag.generation,
            dag_definition_fingerprint=dag.definition_fingerprint,
            sources=tuple(source_values),
            targets=targets,
            created_at=self._clock().astimezone(UTC),
        )

    async def _verify_parent(self, plan: ResultAdoptionPlan, *, allow_desired: bool) -> None:
        snapshot = await self._inspect_parent()
        self._assert_parent_identity(snapshot, plan)
        for target in plan.targets:
            current = _entry_at(snapshot.projection, target.path)
            if current == target.baseline:
                continue
            if allow_desired and current == target.desired:
                continue
            raise ResultAdoptionError(
                f"parent path {target.path!r} changed during adoption verification",
                kind="conflict" if not allow_desired else "concurrent_modification",
            )

    async def _mark_initial_conflicts(self, record: ResultAdoptionRecord) -> None:
        """Persist target-level conflict evidence before terminalizing the plan."""

        try:
            snapshot = await self._inspect_parent()
            self._assert_parent_identity(snapshot, record.plan)
        except ResultAdoptionError:
            return
        for ordinal, target in enumerate(record.plan.targets):
            observed = _entry_at(snapshot.projection, target.path)
            if observed == target.baseline:
                continue
            target_record = await self._store.get_result_adoption_target(
                record.adoption_id,
                ordinal,
            )
            if (
                target_record is None
                or target_record.state is not ResultAdoptionTargetState.NOT_STARTED
            ):
                continue
            await self._transition_target(
                target_record,
                adoption_id=record.adoption_id,
                ordinal=ordinal,
                state=ResultAdoptionTargetState.CONFLICT,
                observed=observed,
                error="conflict",
            )

    @staticmethod
    def _assert_parent_identity(
        snapshot: ParentWorkspaceSnapshot,
        plan: ResultAdoptionPlan,
    ) -> None:
        if (
            snapshot.repository != plan.parent_repository
            or snapshot.projection.head_sha != plan.parent_head_sha
        ):
            raise ResultAdoptionError(
                "parent repository identity or HEAD changed",
                kind="concurrent_modification",
            )

    async def _apply_targets(self, record: ResultAdoptionRecord) -> ResultAdoptionRecord:
        for ordinal, target in enumerate(record.plan.targets):
            current_record = await self._store.get_result_adoption(record.adoption_id)
            if current_record is None:
                raise ResultAdoptionError("adoption record disappeared", kind="integrity")
            target_record = await self._store.get_result_adoption_target(
                record.adoption_id,
                ordinal,
            )
            if target_record is None:
                return await self._terminate(
                    current_record,
                    ResultAdoptionState.INDETERMINATE,
                    ResultAdoptionError("adoption target disappeared", kind="integrity"),
                )
            if target_record.state is ResultAdoptionTargetState.APPLIED:
                continue
            try:
                parent = await self._inspect_parent()
                self._assert_parent_identity(parent, record.plan)
                current = _entry_at(parent.projection, target.path)
            except ResultAdoptionError as error:
                return await self._terminate(
                    current_record,
                    ResultAdoptionState.INDETERMINATE,
                    error,
                )
            if current == target.desired:
                await self._transition_target(
                    target_record,
                    adoption_id=record.adoption_id,
                    ordinal=ordinal,
                    state=ResultAdoptionTargetState.APPLIED,
                    observed=current,
                )
                continue
            if current != target.baseline:
                conflict_state = (
                    ResultAdoptionState.CONFLICT
                    if target_record.state is ResultAdoptionTargetState.NOT_STARTED
                    else ResultAdoptionState.INDETERMINATE
                )
                conflict_error = ResultAdoptionError(
                    f"parent path {target.path!r} is neither expected nor desired",
                    kind="conflict"
                    if conflict_state is ResultAdoptionState.CONFLICT
                    else "concurrent_modification",
                )
                if not target_record.state.terminal:
                    await self._transition_target(
                        target_record,
                        adoption_id=record.adoption_id,
                        ordinal=ordinal,
                        state=(
                            ResultAdoptionTargetState.CONFLICT
                            if conflict_state is ResultAdoptionState.CONFLICT
                            else ResultAdoptionTargetState.INDETERMINATE
                        ),
                        observed=current,
                        error=conflict_error.kind,
                    )
                return await self._terminate(current_record, conflict_state, conflict_error)
            if target_record.state in {
                ResultAdoptionTargetState.NOT_STARTED,
                ResultAdoptionTargetState.RETRYABLE,
            }:
                target_record = await self._transition_target(
                    target_record,
                    adoption_id=record.adoption_id,
                    ordinal=ordinal,
                    state=ResultAdoptionTargetState.APPLYING,
                    observed=current,
                )
            mutation_request = WorkspaceMutationRequest(
                path=target.path,
                operation=target.operation,
                expected=target.baseline,
                desired=target.desired,
            )
            try:
                await self._mutation.apply(
                    mutation_request,
                    session_id=self._parent_session_id,
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                try:
                    after = await self._inspect_parent()
                    self._assert_parent_identity(after, record.plan)
                    observed = _entry_at(after.projection, target.path)
                except ResultAdoptionError as inspect_error:
                    return await self._terminate(
                        current_record,
                        ResultAdoptionState.INDETERMINATE,
                        inspect_error,
                    )
                if observed == target.desired:
                    await self._transition_target(
                        target_record,
                        adoption_id=record.adoption_id,
                        ordinal=ordinal,
                        state=ResultAdoptionTargetState.APPLIED,
                        observed=observed,
                    )
                    continue
                if observed == target.baseline:
                    if "permission" in str(error).casefold() or "denied" in str(error).casefold():
                        await self._transition_target(
                            target_record,
                            adoption_id=record.adoption_id,
                            ordinal=ordinal,
                            state=ResultAdoptionTargetState.FAILED,
                            observed=observed,
                            error="permission_denied",
                        )
                        return await self._terminate(
                            current_record,
                            ResultAdoptionState.FAILED,
                            ResultAdoptionError(str(error), kind="permission_denied"),
                        )
                    await self._transition_target(
                        target_record,
                        adoption_id=record.adoption_id,
                        ordinal=ordinal,
                        state=ResultAdoptionTargetState.RETRYABLE,
                        observed=observed,
                        error=type(error).__name__,
                    )
                    # Keep the parent adoption in APPLYING. A fresh or
                    # repeated controller must be able to re-check the
                    # expected image and retry this target; moving to
                    # VERIFYING here would make the durable RETRYABLE state
                    # immediately terminal on the next pass.
                    refreshed = await self._store.get_result_adoption(record.adoption_id)
                    if refreshed is None:
                        raise ResultAdoptionError(
                            "adoption record disappeared after retryable target transition",
                            kind="integrity",
                        ) from None
                    return refreshed
                return await self._terminate(
                    current_record,
                    ResultAdoptionState.INDETERMINATE,
                    ResultAdoptionError(
                        f"target {target.path!r} is indeterminate after mutation error",
                        kind="concurrent_modification",
                    ),
                )
            try:
                after = await self._inspect_parent()
                self._assert_parent_identity(after, record.plan)
                observed = _entry_at(after.projection, target.path)
            except ResultAdoptionError as error:
                return await self._terminate(
                    current_record,
                    ResultAdoptionState.INDETERMINATE,
                    error,
                )
            if observed != target.desired:
                if observed == target.baseline:
                    await self._transition_target(
                        target_record,
                        adoption_id=record.adoption_id,
                        ordinal=ordinal,
                        state=ResultAdoptionTargetState.RETRYABLE,
                        observed=observed,
                        error="desired_image_not_observed",
                    )
                    # The expected image is still present, so keep the
                    # adoption recoverable instead of converting a safe
                    # retry opportunity into a terminal verification error.
                    refreshed = await self._store.get_result_adoption(record.adoption_id)
                    if refreshed is None:
                        raise ResultAdoptionError(
                            "adoption record disappeared after retryable target transition",
                            kind="integrity",
                        )
                    return refreshed
                return await self._terminate(
                    current_record,
                    ResultAdoptionState.INDETERMINATE,
                    ResultAdoptionError(
                        f"target {target.path!r} is indeterminate after mutation",
                        kind="concurrent_modification",
                    ),
                )
            await self._transition_target(
                target_record,
                adoption_id=record.adoption_id,
                ordinal=ordinal,
                state=ResultAdoptionTargetState.APPLIED,
                observed=observed,
            )
        refreshed = await self._store.get_result_adoption(record.adoption_id)
        if refreshed is None:
            raise ResultAdoptionError("adoption record disappeared", kind="integrity")
        return await self._transition_adoption(refreshed, ResultAdoptionState.VERIFYING)

    async def _complete_verification(self, record: ResultAdoptionRecord) -> ResultAdoptionRecord:
        await self._verify_parent(record.plan, allow_desired=True)
        for ordinal, target in enumerate(record.plan.targets):
            current = await self._store.get_result_adoption_target(record.adoption_id, ordinal)
            if current is None or current.state is not ResultAdoptionTargetState.APPLIED:
                raise ResultAdoptionError("adoption target is not applied", kind="integrity")
            parent = await self._inspect_parent()
            self._assert_parent_identity(parent, record.plan)
            observed = _entry_at(parent.projection, target.path)
            if observed != target.desired:
                with suppress(ResultAdoptionError):
                    await self._transition_target(
                        current,
                        adoption_id=record.adoption_id,
                        ordinal=ordinal,
                        state=ResultAdoptionTargetState.INDETERMINATE,
                        observed=observed,
                        error="post_apply_concurrent_modification",
                    )
                raise ResultAdoptionError(
                    f"target {target.path!r} changed during final verification",
                    kind="concurrent_modification",
                )
        refreshed = await self._store.get_result_adoption(record.adoption_id)
        if refreshed is None:
            raise ResultAdoptionError("adoption record disappeared", kind="integrity")
        return await self._transition_adoption(refreshed, ResultAdoptionState.COMPLETED)

    async def _inspect_parent(self) -> ParentWorkspaceSnapshot:
        try:
            return await self._parent_reader.inspect(self._parent_root)
        except ResultAdoptionError:
            raise
        except (OSError, ValueError, WorkspaceCheckpointError, WorktreeError) as error:
            raise ResultAdoptionError(
                "parent workspace projection is unavailable or changed",
                kind="concurrent_modification",
            ) from error

    async def _transition_adoption(
        self,
        record: ResultAdoptionRecord,
        state: ResultAdoptionState,
        *,
        error_kind: str | None = None,
    ) -> ResultAdoptionRecord:
        proposed = replace(
            record,
            state=state,
            error_kind=error_kind,
            updated_at=self._clock().astimezone(UTC),
            version=record.version + 1,
        )
        return await self._store.transition_result_adoption(
            proposed,
            expected_version=record.version,
            expected_state=record.state,
        )

    async def _transition_target(
        self,
        record: ResultAdoptionTargetRecord,
        *,
        adoption_id: str,
        ordinal: int,
        state: ResultAdoptionTargetState,
        observed: WorkspaceFileEntry | None,
        error: str | None = None,
    ) -> ResultAdoptionTargetRecord:
        proposed = replace(
            record,
            state=state,
            observed_fingerprint=workspace_entry_fingerprint(observed),
            error_kind=error,
            updated_at=self._clock().astimezone(UTC),
            version=record.version + 1,
        )
        return await self._store.transition_result_adoption_target(
            proposed,
            adoption_id=adoption_id,
            ordinal=ordinal,
            owner_pid=self._owner_pid,
            owner_token=self._owner_token,
            expected_version=record.version,
            expected_state=record.state,
        )

    async def _terminate(
        self,
        record: ResultAdoptionRecord,
        state: ResultAdoptionState,
        error: ResultAdoptionError,
    ) -> ResultAdoptionRecord:
        current = await self._store.get_result_adoption(record.adoption_id)
        if current is None:
            raise ResultAdoptionError("adoption record disappeared", kind="integrity")
        if not current.state.terminal:
            current = await self._transition_adoption(
                current,
                state,
                error_kind=error.kind,
            )
        return current

    @staticmethod
    def _conflict_state(error: ResultAdoptionError) -> ResultAdoptionState:
        return (
            ResultAdoptionState.CONFLICT
            if error.kind in {"conflict", "unsafe_path"}
            else ResultAdoptionState.INDETERMINATE
        )


__all__ = ["ResultAdoptionApplicationService"]
