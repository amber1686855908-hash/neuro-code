"""Explicit serialized writable-subagent vertical slice.

This workflow is intentionally separate from the existing read-only subagent
path.  It allocates a clean Neuro-owned worktree from the parent's committed
HEAD, captures a READY baseline, derives a typed child authority, and leaves
all child changes in that worktree for explicit later inspection.
"""

from __future__ import annotations

import asyncio
import math
import os
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast, runtime_checkable

from neuro_code.application.ports.checkpoints import WorkspaceCheckpointApplication
from neuro_code.application.ports.parent_context_relay import ParentContextRelayStore
from neuro_code.application.ports.storage import SessionStore
from neuro_code.application.ports.worktree import WorktreeError
from neuro_code.application.ports.writable_subagent import (
    WritableSubagentLeaseError,
    WritableSubagentLeaseStore,
)
from neuro_code.application.runtime.agent import AgentRunResult, EventSink
from neuro_code.application.runtime.process_liveness import owner_is_alive
from neuro_code.application.workflows.parent_context_relay import (
    ParentContextRelayApplicationService,
)
from neuro_code.application.workflows.subagent_capabilities import (
    MAX_SUBAGENT_STEPS,
    WRITABLE_SUBAGENT_WRITE_TOOL_NAMES,
    SubagentCapabilitySet,
    WritableSubagentCapabilityGrant,
    resolve_writable_subagent_capability,
    writable_subagent_request,
)
from neuro_code.domain.checkpoints import (
    CheckpointCreateRequest,
    CheckpointState,
    workspace_projection_fingerprint,
)
from neuro_code.domain.execution import AgentExecutionOutcome
from neuro_code.domain.parent_context_relay import ParentContextRelay
from neuro_code.domain.session_tasks import (
    SessionTask,
    SessionTaskKind,
    SessionTaskStatus,
    SubagentLink,
)
from neuro_code.domain.task_dag_result_relay import TaskDagDependencyResultRelay
from neuro_code.domain.worktree import (
    WorktreeCreateRequest,
    WorktreeId,
    WorktreeKind,
    WorktreeOwnership,
    WorktreeRepositoryIdentity,
    WorktreeSnapshot,
    WorktreeState,
    WorktreeStatus,
)
from neuro_code.domain.writable_subagent import (
    ManagedChildWorkspaceGrant,
    WritableSubagentLeaseScope,
    WritableSubagentWorkspaceLease,
    WritableSubagentWorkspaceState,
)
from neuro_code.shared.errors import ConfigurationError, SubagentTimeoutError
from neuro_code.shared.redaction import redact_sensitive_text

if TYPE_CHECKING:
    from neuro_code.application.sessions.binding import ConversationBinding

MAX_WRITABLE_SUBAGENT_TIMEOUT_SECONDS = 300.0
MAX_WRITABLE_SUBAGENT_RESULT_BYTES = 32 * 1024


def _now() -> datetime:
    return datetime.now(UTC)


def _bounded_utf8_text(value: str, *, limit: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value, False
    suffix = "..."
    prefix = encoded[: limit - len(suffix)].decode("utf-8", errors="ignore")
    return f"{prefix}{suffix}", True


def _safe_identifier(value: str, *, field_name: str) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\x00" in value
        or len(value.encode("utf-8")) > 512
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{field_name} must be a bounded safe identifier")


def _digest(value: str, *, field_name: str) -> None:
    _safe_identifier(value, field_name=field_name)
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{field_name} must be a SHA-256 digest")


def _same_repository(
    first: WorktreeRepositoryIdentity,
    second: WorktreeRepositoryIdentity,
) -> bool:
    return (
        first.common_dir == second.common_dir
        and first.source_worktree == second.source_worktree
        and first.git_dir == second.git_dir
    )


@runtime_checkable
class WritableWorktreeApplication(Protocol):
    @property
    def managed_root(self) -> Path: ...

    async def initialize(self) -> None: ...

    async def repository_identity(self, path: Path, /) -> WorktreeRepositoryIdentity: ...

    def planned_managed_path(
        self,
        repository: WorktreeRepositoryIdentity,
        worktree_id: WorktreeId,
    ) -> Path: ...

    async def create(self, request: WorktreeCreateRequest) -> WorktreeSnapshot: ...

    async def inspect(self, worktree_id: str, /) -> WorktreeSnapshot: ...

    async def status(self, worktree_id: str, /) -> WorktreeStatus: ...


@runtime_checkable
class WritableSubagentRuntime(Protocol):
    @property
    def child_session_id(self) -> str: ...

    @property
    def capability_fingerprint(self) -> str: ...

    async def run(self, prompt: str, *, sink: EventSink | None = None) -> AgentRunResult: ...

    async def close(self) -> None: ...


@runtime_checkable
class WritableSubagentRuntimeFactory(Protocol):
    async def create_session(
        self,
        request: RunWritableSubagentRequest,
        *,
        capabilities: WritableSubagentCapabilityGrant,
    ) -> str: ...

    async def create(
        self,
        request: RunWritableSubagentRequest,
        *,
        parent_task_id: str,
        child_session_id: str,
        capabilities: WritableSubagentCapabilityGrant,
        relay: ParentContextRelay,
    ) -> WritableSubagentRuntime: ...


@dataclass(frozen=True, slots=True)
class RunWritableSubagentRequest:
    """Explicit opt-in request; the ordinary subagent request remains read-only."""

    parent_session_id: str
    prompt: str
    max_steps: int = 8
    dependency_result_relay: TaskDagDependencyResultRelay | None = None

    def __post_init__(self) -> None:
        _safe_identifier(self.parent_session_id, field_name="parent_session_id")
        if (
            not isinstance(self.prompt, str)
            or not self.prompt.strip()
            or "\x00" in self.prompt
            or len(self.prompt.encode("utf-8")) > 16 * 1024
        ):
            raise ValueError("writable subagent prompt must be non-empty and bounded")
        if any(ord(character) < 32 and character not in "\n\t\r" for character in self.prompt):
            raise ValueError("writable subagent prompt contains an unsafe control character")
        if (
            isinstance(self.max_steps, bool)
            or not isinstance(self.max_steps, int)
            or not 1 <= self.max_steps <= MAX_SUBAGENT_STEPS
        ):
            raise ValueError(
                f"writable subagent max_steps must be between 1 and {MAX_SUBAGENT_STEPS}"
            )
        if self.dependency_result_relay is not None and not isinstance(
            self.dependency_result_relay,
            TaskDagDependencyResultRelay,
        ):
            raise TypeError("writable dependency result relay must be canonical")


@dataclass(frozen=True, slots=True)
class WritableSubagentExecutionIdentity:
    """Internal DAG-to-worker correlation owned by the application layer."""

    dag_id: str
    node_id: str
    parent_task_id: str

    def __post_init__(self) -> None:
        _safe_identifier(self.dag_id, field_name="writable execution DAG id")
        _safe_identifier(self.node_id, field_name="writable execution node id")
        _safe_identifier(self.parent_task_id, field_name="writable execution parent task id")


@dataclass(frozen=True, slots=True)
class WritableSubagentResultProjection:
    """Bounded outcome plus workspace identity; never a diff or transcript."""

    parent_session_id: str
    parent_task_id: str
    child_session_id: str
    status: SessionTaskStatus
    response: str
    steps: int
    outcome: AgentExecutionOutcome | None
    worktree_id: WorktreeId
    baseline_checkpoint_id: str
    base_commit_sha: str
    capability_fingerprint: str
    grant_fingerprint: str
    final_workspace_fingerprint: str | None
    workspace_changed: bool | None
    changed_file_count: int | None
    truncated: bool

    def __post_init__(self) -> None:
        for value, field_name in (
            (self.parent_session_id, "parent_session_id"),
            (self.parent_task_id, "parent_task_id"),
            (self.child_session_id, "child_session_id"),
            (self.baseline_checkpoint_id, "baseline_checkpoint_id"),
        ):
            _safe_identifier(value, field_name=field_name)
        if not isinstance(self.status, SessionTaskStatus) or not self.status.terminal:
            raise ValueError("writable result status must be terminal")
        if (
            not isinstance(self.response, str)
            or len(self.response.encode("utf-8")) > MAX_WRITABLE_SUBAGENT_RESULT_BYTES
        ):
            raise ValueError("writable result response is too large")
        if isinstance(self.steps, bool) or not isinstance(self.steps, int) or self.steps < 0:
            raise ValueError("writable result steps must be non-negative")
        if self.outcome is not None and not isinstance(self.outcome, AgentExecutionOutcome):
            raise ValueError("writable result outcome must be canonical")
        if not isinstance(self.worktree_id, WorktreeId):
            raise TypeError("writable result worktree id must be canonical")
        if not isinstance(self.base_commit_sha, str) or not self.base_commit_sha:
            raise ValueError("writable result base commit must be non-empty")
        _digest(self.capability_fingerprint, field_name="writable result capability fingerprint")
        _digest(self.grant_fingerprint, field_name="writable result grant fingerprint")
        if self.final_workspace_fingerprint is not None:
            _digest(self.final_workspace_fingerprint, field_name="final workspace fingerprint")
        if self.workspace_changed is not None and not isinstance(self.workspace_changed, bool):
            raise TypeError("writable result changed flag must be boolean or None")
        if self.changed_file_count is not None and (
            isinstance(self.changed_file_count, bool)
            or not isinstance(self.changed_file_count, int)
            or self.changed_file_count < 0
        ):
            raise ValueError("writable result changed file count must be non-negative")
        if not isinstance(self.truncated, bool):
            raise TypeError("writable result truncated flag must be boolean")


class WritableSubagentApplicationService:
    """Allocate, run, preserve, and reconcile one serialized writable child."""

    def __init__(
        self,
        store: SessionStore,
        lease_store: WritableSubagentLeaseStore,
        worktrees: WritableWorktreeApplication,
        checkpoints: WorkspaceCheckpointApplication,
        runtime_factory: WritableSubagentRuntimeFactory,
        *,
        parent_binding: ConversationBinding,
        global_policy: SubagentCapabilitySet,
        relay_store: ParentContextRelayStore | None = None,
        redaction_values: Iterable[str] = (),
        timeout_seconds: float = 120.0,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        if not isinstance(global_policy, SubagentCapabilitySet):
            raise ConfigurationError("global subagent capability policy is required")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(float(timeout_seconds))
            or not 0 < timeout_seconds <= MAX_WRITABLE_SUBAGENT_TIMEOUT_SECONDS
        ):
            raise ValueError("writable subagent timeout is out of bounds")
        if not isinstance(worktrees, WritableWorktreeApplication):
            raise ConfigurationError("writable worktree application is invalid")
        if not isinstance(runtime_factory, WritableSubagentRuntimeFactory):
            raise ConfigurationError("writable subagent runtime factory is invalid")
        from neuro_code.application.sessions.binding import (
            ConversationBinding as CanonicalConversationBinding,
        )

        if not isinstance(parent_binding, CanonicalConversationBinding):
            raise ConfigurationError("writable subagent parent binding is required")
        parent_capabilities = parent_binding.capabilities
        if not isinstance(parent_capabilities, SubagentCapabilitySet):
            raise ConfigurationError(
                "writable subagent parent binding capability metadata is missing"
            )
        parent_session_id = parent_binding.runner.session_id
        if (
            not isinstance(parent_session_id, str)
            or not parent_session_id.strip()
            or "\x00" in parent_session_id
        ):
            raise ConfigurationError("writable subagent parent binding session identity is missing")
        if (
            not parent_capabilities.filesystem_write
            or not parent_capabilities.sandbox_profile.workspace_writable
            or not WRITABLE_SUBAGENT_WRITE_TOOL_NAMES.issubset(
                parent_capabilities.allowed_tool_names
            )
        ):
            raise ConfigurationError(
                "writable subagent parent binding does not carry writable authority"
            )
        self._store = store
        self._lease_store = lease_store
        self._worktrees = worktrees
        self._checkpoints = checkpoints
        self._runtime_factory = runtime_factory
        self._parent_binding = parent_binding
        self._parent_session_id = parent_session_id
        self._parent_capabilities = parent_capabilities
        self._global_policy = global_policy
        resolved_relay_store = relay_store or cast(ParentContextRelayStore, store)
        self._relay_service = ParentContextRelayApplicationService(
            store,
            resolved_relay_store,
            parent_binding=parent_binding,
            redaction_values=redaction_values,
            clock=clock,
        )
        self._relay_store = resolved_relay_store
        self._timeout_seconds = float(timeout_seconds)
        self._clock = clock
        self._lock = asyncio.Lock()
        self._owner_token = f"writable-owner-{uuid.uuid4().hex}"
        self._initialized = False

    @property
    def parent_session_id(self) -> str:
        """The actual parent binding identity captured by this service."""

        return self._parent_session_id

    async def initialize(self) -> None:
        if self._initialized:
            return
        await self._lease_store.initialize()
        await self._relay_store.initialize()
        await self._worktrees.initialize()
        await self._checkpoints.initialize()
        self._initialized = True

    async def run_subagent(
        self,
        request: RunWritableSubagentRequest,
        *,
        sink: EventSink | None = None,
    ) -> WritableSubagentResultProjection:
        if not isinstance(request, RunWritableSubagentRequest):
            raise ValueError("writable subagent request must be canonical")
        if request.parent_session_id != self._parent_session_id:
            raise ConfigurationError("writable subagent request parent session does not match")
        if request.dependency_result_relay is not None:
            raise ConfigurationError(
                "DAG dependency result relay requires an exact DAG execution identity"
            )
        if not self._initialized:
            raise ConfigurationError("writable subagent service is not initialized")
        requested = writable_subagent_request(
            self._parent_capabilities,
            global_policy=self._global_policy,
            max_steps=request.max_steps,
        )
        async with self._lock:
            return await self._run_locked(request, requested, sink=sink)

    async def run_subagent_with_execution_identity(
        self,
        request: RunWritableSubagentRequest,
        *,
        execution_identity: WritableSubagentExecutionIdentity,
        sink: EventSink | None = None,
    ) -> WritableSubagentResultProjection:
        """Run one DAG node with its pre-persisted exact SessionTask id.

        This is an application-internal seam.  Ordinary writable callers keep
        the generated task identity used by :meth:`run_subagent`.
        """

        if not isinstance(request, RunWritableSubagentRequest):
            raise ValueError("writable subagent request must be canonical")
        if not isinstance(execution_identity, WritableSubagentExecutionIdentity):
            raise ConfigurationError("writable execution identity must be canonical")
        if request.parent_session_id != self._parent_session_id:
            raise ConfigurationError("writable subagent request parent session does not match")
        if request.dependency_result_relay is not None and (
            request.dependency_result_relay.dag_id != execution_identity.dag_id
            or request.dependency_result_relay.target_node_id != execution_identity.node_id
            or request.dependency_result_relay.target_node_generation < 1
        ):
            raise ConfigurationError("writable dependency result relay identity is inconsistent")
        if not self._initialized:
            raise ConfigurationError("writable subagent service is not initialized")
        requested = writable_subagent_request(
            self._parent_capabilities,
            global_policy=self._global_policy,
            max_steps=request.max_steps,
        )
        async with self._lock:
            return await self._run_locked(
                request,
                requested,
                sink=sink,
                parent_task_id=execution_identity.parent_task_id,
                dependency_result_relay=request.dependency_result_relay,
                execution_scope=WritableSubagentLeaseScope.TASK_DAG,
            )

    async def _run_locked(
        self,
        request: RunWritableSubagentRequest,
        requested: SubagentCapabilitySet,
        *,
        sink: EventSink | None,
        parent_task_id: str | None = None,
        dependency_result_relay: TaskDagDependencyResultRelay | None = None,
        execution_scope: WritableSubagentLeaseScope = WritableSubagentLeaseScope.STANDALONE,
    ) -> WritableSubagentResultProjection:
        parent_capabilities = self._parent_capabilities
        parent_session_id = self._parent_session_id
        if dependency_result_relay != request.dependency_result_relay:
            raise ConfigurationError("writable dependency result relay was not preserved")
        parent_repository = await self._worktrees.repository_identity(parent_capabilities.cwd)
        worktree_id = WorktreeId.new()
        child_root = self._worktrees.planned_managed_path(parent_repository, worktree_id)
        now = self._clock().astimezone(UTC)
        lease = WritableSubagentWorkspaceLease(
            lease_id=f"wsl-{uuid.uuid4().hex}",
            parent_session_id=parent_session_id,
            parent_task_id=parent_task_id or f"writable-subagent-{uuid.uuid4().hex}",
            worktree_id=worktree_id,
            parent_capability_fingerprint=parent_capabilities.fingerprint,
            parent_workspace_root=parent_capabilities.cwd,
            parent_repository=parent_repository,
            base_commit_sha=parent_repository.head_sha,
            canonical_child_root=child_root,
            state=WritableSubagentWorkspaceState.ALLOCATING,
            created_at=now,
            updated_at=now,
            owner_pid=os.getpid(),
            owner_token=self._owner_token,
            execution_scope=execution_scope,
        )
        try:
            lease = await self._lease_store.insert_writable_subagent_lease(lease)
        except WritableSubagentLeaseError as error:
            raise ConfigurationError(f"writable subagent allocation denied: {error}") from error

        task = SessionTask(
            lease.parent_task_id,
            SessionTaskKind.SUBAGENT,
            SessionTaskStatus.RUNNING,
            now,
        )
        runtime: WritableSubagentRuntime | None = None
        result: AgentRunResult | None = None
        failure: BaseException | None = None
        task_created = False
        try:
            await self._store.create_session_task(parent_session_id, task)
            task_created = True
            lease = await self._create_worktree(lease)
            snapshot = lease.worktree
            if snapshot is None:
                raise ConfigurationError("writable worktree handle was not persisted")
            checkpoint = await self._checkpoints.create(CheckpointCreateRequest(snapshot))
            if checkpoint.state is not CheckpointState.READY:
                raise ConfigurationError("writable baseline checkpoint did not become READY")
            grant = ManagedChildWorkspaceGrant(
                grant_id=lease.lease_id,
                parent_capability_fingerprint=parent_capabilities.fingerprint,
                parent_workspace_root=parent_capabilities.cwd,
                parent_repository=parent_repository,
                base_commit_sha=lease.base_commit_sha,
                worktree=snapshot,
                managed_worktree_id=lease.worktree_id,
                canonical_child_root=lease.canonical_child_root,
                created_at=lease.created_at,
                baseline_checkpoint_id=checkpoint.checkpoint_id,
            )
            effective = resolve_writable_subagent_capability(
                parent=parent_capabilities,
                requested=requested,
                global_policy=self._global_policy,
                workspace_grant=grant,
            )
            lease = await self._transition(
                lease,
                replace(
                    lease,
                    state=WritableSubagentWorkspaceState.BASELINE_READY,
                    updated_at=self._clock().astimezone(UTC),
                    baseline_checkpoint_id=checkpoint.checkpoint_id,
                    capability_fingerprint=effective.capabilities.fingerprint,
                    grant_fingerprint=grant.fingerprint,
                ),
            )
            child_session_id = await self._runtime_factory.create_session(
                request,
                capabilities=effective,
            )
            _safe_identifier(child_session_id, field_name="child_session_id")
            lease = await self._transition(
                lease,
                replace(
                    lease,
                    child_session_id=child_session_id,
                    updated_at=self._clock().astimezone(UTC),
                ),
            )
            await self._store.save_subagent_link(
                SubagentLink(
                    parent_session_id,
                    task.task_id,
                    child_session_id,
                    self._clock().astimezone(UTC),
                )
            )
            relay = await self._relay_service.publish(lease, prompt=request.prompt)
            runtime = await self._runtime_factory.create(
                request,
                parent_task_id=task.task_id,
                child_session_id=child_session_id,
                capabilities=effective,
                relay=relay,
            )
            if runtime.capability_fingerprint != effective.fingerprint:
                raise ConfigurationError(
                    "writable child runtime capability metadata is inconsistent"
                )
            if runtime.child_session_id != child_session_id:
                raise ConfigurationError("writable child runtime session identity is inconsistent")
            lease = await self._transition(
                lease,
                replace(
                    lease,
                    state=WritableSubagentWorkspaceState.ACTIVE,
                    updated_at=self._clock().astimezone(UTC),
                ),
            )
            async with asyncio.timeout(self._timeout_seconds):
                result = await runtime.run(request.prompt, sink=sink)
            if result.session_id != child_session_id:
                raise ConfigurationError(
                    "writable child runtime returned a different child session"
                )
        except asyncio.CancelledError as error:
            failure = error
            await asyncio.shield(self._close_runtime(runtime))
            if task_created:
                await asyncio.shield(
                    self._finish_after_run(lease, task, SessionTaskStatus.CANCELLED, error)
                )
            else:
                await asyncio.shield(self._mark_unstarted_lease_failed(lease, error))
            raise
        except TimeoutError as error:
            failure = SubagentTimeoutError(
                f"writable subagent exceeded its {self._timeout_seconds:g}-second wall-clock limit"
            )
            await self._close_runtime(runtime)
            if task_created:
                await self._finish_after_run(lease, task, SessionTaskStatus.FAILED, failure)
            else:
                await self._mark_unstarted_lease_failed(lease, failure)
            raise failure from error
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            failure = error
            await self._close_runtime(runtime)
            if task_created:
                await self._finish_after_run(lease, task, SessionTaskStatus.FAILED, error)
            else:
                await self._mark_unstarted_lease_failed(lease, error)
            raise
        else:
            close_error = await self._close_runtime(runtime)
            if close_error is not None:
                failure = close_error
            terminal = (
                SessionTaskStatus.FAILED if failure is not None else SessionTaskStatus.COMPLETED
            )
            lease, final_task = await self._finish_after_run(lease, task, terminal, failure)
            if result is None:
                raise ConfigurationError("writable child completed without a run result")
            return self._project_result(
                final_task,
                result,
                lease,
                response=result.response,
                truncated=False,
            )

    async def _mark_unstarted_lease_failed(
        self,
        lease: WritableSubagentWorkspaceLease,
        failure: BaseException,
    ) -> WritableSubagentWorkspaceLease:
        if lease.state.terminal:
            return lease
        return await self._transition(
            lease,
            replace(
                lease,
                state=WritableSubagentWorkspaceState.FAILED,
                updated_at=self._clock().astimezone(UTC),
                error_kind=type(failure).__name__,
            ),
        )

    async def _create_worktree(
        self,
        lease: WritableSubagentWorkspaceLease,
    ) -> WritableSubagentWorkspaceLease:
        branch = f"neuro/writable-subagent/{lease.worktree_id.value}"
        snapshot = await self._worktrees.create(
            WorktreeCreateRequest(
                lease.parent_repository.source_worktree,
                lease.base_commit_sha,
                kind=WorktreeKind.MANAGED_BRANCH,
                worktree_id=lease.worktree_id,
                branch=branch,
                created_by_session_id=lease.parent_session_id,
            )
        )
        if (
            snapshot.state is not WorktreeState.READY
            or snapshot.ownership is not WorktreeOwnership.MANAGED
            or snapshot.worktree_id != lease.worktree_id
            or snapshot.canonical_path != lease.canonical_child_root
            or snapshot.base_commit_sha != lease.base_commit_sha
            or not _same_repository(snapshot.repository, lease.parent_repository)
        ):
            raise ConfigurationError("created writable worktree failed its ownership proof")
        return await self._transition(
            lease,
            replace(
                lease,
                state=WritableSubagentWorkspaceState.WORKTREE_READY,
                worktree=snapshot.handle,
                updated_at=self._clock().astimezone(UTC),
            ),
        )

    async def _finish_after_run(
        self,
        lease: WritableSubagentWorkspaceLease,
        task: SessionTask,
        status: SessionTaskStatus,
        failure: BaseException | None,
    ) -> tuple[WritableSubagentWorkspaceLease, SessionTask]:
        lease = await self._preserve_workspace(lease, failure)
        finished = task.finish(status, finished_at=self._clock().astimezone(UTC))
        await self._store.update_session_task(lease.parent_session_id, finished)
        return lease, finished

    async def _preserve_workspace(
        self,
        lease: WritableSubagentWorkspaceLease,
        failure: BaseException | None,
    ) -> WritableSubagentWorkspaceLease:
        if lease.state.terminal:
            return lease
        error_kind = type(failure).__name__ if failure is not None else None
        if lease.worktree is None or lease.baseline_checkpoint_id is None:
            return await self._transition(
                lease,
                replace(
                    lease,
                    state=WritableSubagentWorkspaceState.FAILED,
                    updated_at=self._clock().astimezone(UTC),
                    error_kind=error_kind or "allocation_failed",
                ),
            )
        try:
            checkpoint = await self._checkpoints.get(lease.baseline_checkpoint_id)
            if checkpoint is None or checkpoint.state is not CheckpointState.READY:
                raise ConfigurationError("writable baseline checkpoint is not READY")
            projection = await self._checkpoints.inspect(lease.worktree)
            final_fingerprint = workspace_projection_fingerprint(lease.worktree, projection).value
            status = await self._worktrees.status(lease.worktree_id.value)
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            return await self._transition(
                lease,
                replace(
                    lease,
                    state=WritableSubagentWorkspaceState.ORPHANED,
                    updated_at=self._clock().astimezone(UTC),
                    error_kind=type(error).__name__,
                ),
            )
        return await self._transition(
            lease,
            replace(
                lease,
                state=WritableSubagentWorkspaceState.PRESERVED,
                updated_at=self._clock().astimezone(UTC),
                final_workspace_fingerprint=final_fingerprint,
                workspace_changed=final_fingerprint != checkpoint.source_fingerprint.value,
                changed_file_count=status.changed_file_count,
                error_kind=error_kind,
            ),
        )

    async def _close_runtime(
        self,
        runtime: WritableSubagentRuntime | None,
    ) -> BaseException | None:
        if runtime is None:
            return None
        try:
            await asyncio.shield(runtime.close())
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            return error
        return None

    async def _transition(
        self,
        lease: WritableSubagentWorkspaceLease,
        proposed: WritableSubagentWorkspaceLease,
    ) -> WritableSubagentWorkspaceLease:
        try:
            return await self._lease_store.compare_and_transition_writable_subagent_lease(
                proposed,
                expected_version=lease.version,
                expected_state=lease.state,
            )
        except WritableSubagentLeaseError as error:
            raise ConfigurationError(
                f"writable subagent lease transition failed: {error}"
            ) from error

    def _project_result(
        self,
        task: SessionTask,
        result: AgentRunResult,
        lease: WritableSubagentWorkspaceLease,
        *,
        response: str,
        truncated: bool,
    ) -> WritableSubagentResultProjection:
        if lease.child_session_id is None or lease.baseline_checkpoint_id is None:
            raise ConfigurationError("writable result linkage is incomplete")
        safe_response = redact_sensitive_text(response)
        safe_response, response_truncated = _bounded_utf8_text(
            safe_response,
            limit=MAX_WRITABLE_SUBAGENT_RESULT_BYTES,
        )
        if lease.capability_fingerprint is None or lease.grant_fingerprint is None:
            raise ConfigurationError("writable result capability linkage is incomplete")
        return WritableSubagentResultProjection(
            parent_session_id=lease.parent_session_id,
            parent_task_id=task.task_id,
            child_session_id=lease.child_session_id,
            status=task.status,
            response=safe_response,
            steps=result.steps,
            outcome=result.outcome,
            worktree_id=lease.worktree_id,
            baseline_checkpoint_id=lease.baseline_checkpoint_id.value,
            base_commit_sha=lease.base_commit_sha,
            capability_fingerprint=lease.effective_fingerprint or lease.capability_fingerprint,
            grant_fingerprint=lease.grant_fingerprint,
            final_workspace_fingerprint=lease.final_workspace_fingerprint,
            workspace_changed=lease.workspace_changed,
            changed_file_count=lease.changed_file_count,
            truncated=truncated or response_truncated,
        )

    async def reconcile_writable_subagent_workspaces(
        self,
    ) -> tuple[WritableSubagentWorkspaceLease, ...]:
        """Classify durable leases after crashes without removing uncertain work."""

        await self.initialize()
        leases = await self._lease_store.list_writable_subagent_leases(include_terminal=False)
        reconciled: list[WritableSubagentWorkspaceLease] = []
        for lease in leases:
            current = lease
            try:
                snapshot = await self._worktrees.inspect(lease.worktree_id.value)
            except (WorktreeError, ConfigurationError):
                if lease.state is WritableSubagentWorkspaceState.ALLOCATING and not _owner_alive(
                    lease.owner_pid
                ):
                    current = await self._transition(
                        lease,
                        replace(
                            lease,
                            state=WritableSubagentWorkspaceState.FAILED,
                            updated_at=self._clock().astimezone(UTC),
                            error_kind="allocation_intent_without_worktree",
                        ),
                    )
                else:
                    current = await self._transition(
                        lease,
                        replace(
                            lease,
                            state=WritableSubagentWorkspaceState.ORPHANED,
                            updated_at=self._clock().astimezone(UTC),
                            error_kind="managed_worktree_unavailable",
                        ),
                    )
                reconciled.append(current)
                continue
            if (
                snapshot.state is not WorktreeState.READY
                or snapshot.ownership is not WorktreeOwnership.MANAGED
                or snapshot.worktree_id != lease.worktree_id
                or snapshot.canonical_path != lease.canonical_child_root
                or snapshot.base_commit_sha != lease.base_commit_sha
                or not _same_repository(snapshot.repository, lease.parent_repository)
            ):
                current = await self._transition(
                    lease,
                    replace(
                        lease,
                        state=WritableSubagentWorkspaceState.ORPHANED,
                        updated_at=self._clock().astimezone(UTC),
                        error_kind="worktree_identity_or_state_mismatch",
                    ),
                )
                reconciled.append(current)
                continue
            if (
                lease.worktree is None
                and lease.state is WritableSubagentWorkspaceState.WORKTREE_READY
            ):
                current = await self._transition(
                    lease,
                    replace(
                        lease,
                        state=WritableSubagentWorkspaceState.WORKTREE_READY,
                        worktree=snapshot.handle,
                        updated_at=self._clock().astimezone(UTC),
                    ),
                )
            elif lease.worktree is None:
                current = await self._transition(
                    lease,
                    replace(
                        lease,
                        state=WritableSubagentWorkspaceState.ORPHANED,
                        updated_at=self._clock().astimezone(UTC),
                        error_kind="persisted_worktree_handle_missing",
                    ),
                )
                reconciled.append(current)
                continue
            if current.state in {
                WritableSubagentWorkspaceState.BASELINE_READY,
                WritableSubagentWorkspaceState.ACTIVE,
            }:
                if current.baseline_checkpoint_id is None:
                    current = await self._transition(
                        current,
                        replace(
                            current,
                            state=WritableSubagentWorkspaceState.ORPHANED,
                            updated_at=self._clock().astimezone(UTC),
                            error_kind="baseline_checkpoint_link_missing",
                        ),
                    )
                    reconciled.append(current)
                    continue
                checkpoint = await self._checkpoints.get(current.baseline_checkpoint_id)
                if checkpoint is None or checkpoint.state is not CheckpointState.READY:
                    current = await self._transition(
                        current,
                        replace(
                            current,
                            state=WritableSubagentWorkspaceState.ORPHANED,
                            updated_at=self._clock().astimezone(UTC),
                            error_kind="baseline_checkpoint_unavailable",
                        ),
                    )
                    reconciled.append(current)
                    continue
            if current.state is WritableSubagentWorkspaceState.WORKTREE_READY:
                if not _owner_alive(current.owner_pid):
                    current = await self._transition(
                        current,
                        replace(
                            current,
                            state=WritableSubagentWorkspaceState.ORPHANED,
                            updated_at=self._clock().astimezone(UTC),
                            error_kind="worktree_ready_without_baseline",
                        ),
                    )
            elif current.state in {
                WritableSubagentWorkspaceState.BASELINE_READY,
                WritableSubagentWorkspaceState.ACTIVE,
            } and not _owner_alive(current.owner_pid):
                current = await self._transition(
                    current,
                    replace(
                        current,
                        state=WritableSubagentWorkspaceState.ORPHANED,
                        updated_at=self._clock().astimezone(UTC),
                        error_kind="dead_writable_subagent_owner",
                    ),
                )
            reconciled.append(current)
        return tuple(reconciled)


_owner_alive = owner_is_alive


__all__ = [
    "MAX_WRITABLE_SUBAGENT_RESULT_BYTES",
    "MAX_WRITABLE_SUBAGENT_TIMEOUT_SECONDS",
    "RunWritableSubagentRequest",
    "WritableSubagentApplicationService",
    "WritableSubagentExecutionIdentity",
    "WritableSubagentResultProjection",
    "WritableSubagentRuntime",
    "WritableSubagentRuntimeFactory",
]
