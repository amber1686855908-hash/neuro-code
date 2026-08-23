"""Application service for bounded managed-workspace checkpoint/rollback."""

from __future__ import annotations

import asyncio
import ctypes
import os
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime
from typing import cast

from neuro_code.application.ports.checkpoints import (
    MAX_CHECKPOINT_CAPTURE_SECONDS,
    CheckpointArtifactStore,
    CheckpointFailureKind,
    WorkspaceCheckpointError,
    WorkspaceCheckpointStore,
    WorkspaceGitPort,
    WorkspaceStatePort,
)
from neuro_code.application.ports.worktree import (
    MINIMUM_GIT_VERSION,
    GitWorktreePort,
    ManagedWorktreeStore,
    WorktreeError,
)
from neuro_code.domain.checkpoints import (
    CheckpointCreateRequest,
    CheckpointId,
    CheckpointState,
    RollbackAttempt,
    RollbackAttemptId,
    RollbackState,
    WorkspaceCheckpoint,
    WorkspaceProjection,
    workspace_projection_fingerprint,
)
from neuro_code.domain.worktree import (
    WorktreeHandle,
    WorktreeOwnership,
    WorktreeRepositoryIdentity,
    WorktreeSnapshot,
    WorktreeState,
    WorktreeStatus,
)

Clock = Callable[[], datetime]

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_SYNCHRONIZE = 0x00100000
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 0x102
_ERROR_FILE_NOT_FOUND = 2
_ERROR_INVALID_PARAMETER = 87


def _now() -> datetime:
    return datetime.now(UTC)


def _same_repository(
    first: WorktreeSnapshot,
    second_repository: WorktreeRepositoryIdentity,
) -> bool:
    return (
        first.repository.common_dir == second_repository.common_dir
        and first.repository.source_worktree == second_repository.source_worktree
        and first.repository.git_dir == second_repository.git_dir
    )


def _owner_is_alive(pid: int | None) -> bool:
    if pid is None or pid <= 0:
        return False
    if os.name == "nt":
        return _windows_owner_is_alive(pid)
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (ProcessLookupError, OSError):
        return False
    return True


def _windows_owner_is_alive(pid: int) -> bool:
    """Observe a Windows PID through its signalled process object."""

    loader = getattr(ctypes, "WinDLL", None)
    get_last_error = getattr(ctypes, "get_last_error", None)
    if loader is None or get_last_error is None:  # pragma: no cover - Windows-only runtime boundary
        return True
    read_last_error = cast(Callable[[], int], get_last_error)
    try:
        kernel32 = loader("kernel32.dll", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = [ctypes.c_uint32, ctypes.c_int32, ctypes.c_uint32]
        open_process.restype = ctypes.c_void_p
        wait_for_single_object = kernel32.WaitForSingleObject
        wait_for_single_object.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        wait_for_single_object.restype = ctypes.c_uint32
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [ctypes.c_void_p]
        close_handle.restype = ctypes.c_int32
        handle = open_process(
            _PROCESS_QUERY_LIMITED_INFORMATION | _SYNCHRONIZE,
            False,
            pid,
        )
    except (AttributeError, OSError):  # pragma: no cover - Windows-only fallback
        return True
    if not handle:
        error = read_last_error()
        return error not in {_ERROR_FILE_NOT_FOUND, _ERROR_INVALID_PARAMETER}
    try:
        result = cast(int, wait_for_single_object(handle, 0))
        if result == _WAIT_TIMEOUT:
            return True
        return result != _WAIT_OBJECT_0
    finally:
        close_handle(handle)


class WorkspaceCheckpointApplicationService:
    """Capture immutable source projections and retry-safe owned rollbacks."""

    def __init__(
        self,
        *,
        git: GitWorktreePort,
        workspace_git: WorkspaceGitPort,
        worktrees: ManagedWorktreeStore,
        state: WorkspaceStatePort,
        checkpoints: WorkspaceCheckpointStore,
        artifacts: CheckpointArtifactStore,
        clock: Clock = _now,
        checkpoint_id_factory: Callable[[], CheckpointId] = CheckpointId.new,
        attempt_id_factory: Callable[[], RollbackAttemptId] = RollbackAttemptId.new,
    ) -> None:
        self._git = git
        self._workspace_git = workspace_git
        self._worktrees = worktrees
        self._state = state
        self._checkpoints = checkpoints
        self._artifacts = artifacts
        self._clock = clock
        self._checkpoint_id_factory = checkpoint_id_factory
        self._attempt_id_factory = attempt_id_factory
        self._owner_token = f"owner-{uuid.uuid4().hex}"
        self._initialized = False
        self._worktree_locks: dict[str, asyncio.Lock] = {}
        self._worktree_locks_guard = asyncio.Lock()

    async def initialize(self) -> None:
        await self._worktrees.initialize()
        await self._checkpoints.initialize()
        await self._artifacts.initialize()
        version = await self._git.git_version()
        if version < MINIMUM_GIT_VERSION:
            raise WorkspaceCheckpointError(
                "installed Git is below the managed workspace checkpoint minimum",
                kind=CheckpointFailureKind.NOT_AVAILABLE,
            )
        self._initialized = True

    async def create(self, request: CheckpointCreateRequest) -> WorkspaceCheckpoint:
        self._require_initialized()
        if not isinstance(request, CheckpointCreateRequest):
            raise TypeError("workspace checkpoint create accepts a canonical request")
        handle = request.worktree
        lock = await self._worktree_lock(handle.worktree_id.value)
        async with lock:
            snapshot, _ = await self._prove_handle(handle, allow_lock_reason=None)
            projection = await self._capture(handle)
            fingerprint = workspace_projection_fingerprint(handle, projection)
            checkpoint_id = request.checkpoint_id or self._checkpoint_id_factory()
            if not isinstance(checkpoint_id, CheckpointId):
                raise TypeError("checkpoint id factory must return CheckpointId")
            intent = WorkspaceCheckpoint(
                checkpoint_id=checkpoint_id,
                worktree_id=handle.worktree_id,
                repository=snapshot.repository,
                canonical_path=handle.path,
                head_sha=projection.head_sha,
                branch=projection.branch,
                detached=projection.detached,
                created_at=self._clock().astimezone(UTC),
                source_fingerprint=fingerprint,
                artifact_path=self._artifacts.path_for(checkpoint_id),
                artifact_sha256="0" * 64,
                artifact_bytes=0,
                artifact_file_count=0,
                state=CheckpointState.CAPTURING,
            )
            inserted = False
            try:
                await self._checkpoints.insert_capturing(intent)
                inserted = True
                published = await self._artifacts.publish(intent, projection)
                ready = replace(published, state=CheckpointState.READY)
                return await self._checkpoints.compare_and_transition_checkpoint(
                    ready,
                    expected_version=intent.version,
                    expected_state=CheckpointState.CAPTURING,
                )
            except BaseException:
                if inserted:
                    current = await self._checkpoints.get(checkpoint_id)
                    if current is not None and current.state is CheckpointState.CAPTURING:
                        with suppress(WorkspaceCheckpointError):
                            await self._checkpoints.compare_and_transition_checkpoint(
                                replace(current, state=CheckpointState.FAILED),
                                expected_version=current.version,
                                expected_state=CheckpointState.CAPTURING,
                            )
                await self._artifacts.remove_temporary_capture(checkpoint_id)
                raise

    async def inspect(self, handle: WorktreeHandle, /) -> WorkspaceProjection:
        """Inspect one owned worktree without creating or restoring a checkpoint."""

        self._require_initialized()
        if not isinstance(handle, WorktreeHandle):
            raise TypeError("workspace inspection requires a canonical worktree handle")
        lock = await self._worktree_lock(handle.worktree_id.value)
        async with lock:
            _, status = await self._prove_handle(handle, allow_lock_reason=None)
            if status.locked:
                raise WorkspaceCheckpointError(
                    "locked managed worktree cannot be inspected for a writable result",
                    kind=CheckpointFailureKind.LOCKED,
                )
            return await self._capture(handle)

    async def get(self, checkpoint_id: CheckpointId, /) -> WorkspaceCheckpoint | None:
        """Read one checkpoint metadata record for internal reconciliation."""

        self._require_initialized()
        if not isinstance(checkpoint_id, CheckpointId):
            raise TypeError("checkpoint id must be canonical")
        return await self._checkpoints.get(checkpoint_id)

    async def rollback(
        self,
        checkpoint_id: CheckpointId,
        *,
        attempt_id: RollbackAttemptId | None = None,
    ) -> RollbackAttempt:
        self._require_initialized()
        if not isinstance(checkpoint_id, CheckpointId):
            raise TypeError("checkpoint id must be canonical")
        if attempt_id is not None and not isinstance(attempt_id, RollbackAttemptId):
            raise TypeError("rollback attempt id must be canonical")
        checkpoint = await self._checkpoints.get(checkpoint_id)
        if checkpoint is None or checkpoint.state is not CheckpointState.READY:
            raise WorkspaceCheckpointError(
                "checkpoint is not a ready Neuro Code-owned target",
                kind=CheckpointFailureKind.UNMANAGED,
            )
        projection = await self._artifacts.load(checkpoint)
        snapshot = await self._worktrees.get(checkpoint.worktree_id.value)
        if snapshot is None:
            raise WorkspaceCheckpointError(
                "checkpoint worktree ownership record is missing",
                kind=CheckpointFailureKind.UNMANAGED,
            )
        lock = await self._worktree_lock(snapshot.worktree_id.value)
        async with lock:
            current_snapshot, status = await self._prove_handle(
                snapshot.handle, allow_lock_reason=None
            )
            del current_snapshot
            if status.head_sha != checkpoint.head_sha:
                raise WorkspaceCheckpointError(
                    "managed worktree HEAD no longer matches checkpoint HEAD",
                    kind=CheckpointFailureKind.HEAD_MISMATCH,
                )
            if status.locked:
                raise WorkspaceCheckpointError(
                    "managed worktree is already locked by another owner",
                    kind=CheckpointFailureKind.LOCKED,
                )
            attempt = await self._get_or_start_attempt(
                checkpoint,
                snapshot,
                attempt_id,
            )
            return await self._resume_attempt(checkpoint, projection, snapshot, attempt)

    async def reconcile(self) -> tuple[RollbackAttempt, ...]:
        self._require_initialized()
        for checkpoint in await self._checkpoints.list(include_failed=True):
            if checkpoint.state is not CheckpointState.CAPTURING:
                continue
            try:
                recovered = await self._artifacts.recover(checkpoint)
                await self._checkpoints.compare_and_transition_checkpoint(
                    replace(recovered, state=CheckpointState.READY),
                    expected_version=checkpoint.version,
                    expected_state=CheckpointState.CAPTURING,
                )
            except BaseException:
                current = await self._checkpoints.get(checkpoint.checkpoint_id)
                if current is not None and current.state is CheckpointState.CAPTURING:
                    with suppress(WorkspaceCheckpointError):
                        await self._checkpoints.compare_and_transition_checkpoint(
                            replace(current, state=CheckpointState.FAILED),
                            expected_version=current.version,
                            expected_state=CheckpointState.CAPTURING,
                        )
                await self._artifacts.remove_temporary_capture(checkpoint.checkpoint_id)
        results: list[RollbackAttempt] = []
        for active in await self._checkpoints.list_active_attempts():
            active_checkpoint = await self._checkpoints.get(active.checkpoint_id)
            snapshot = await self._worktrees.get(active.worktree_id.value)
            if (
                active_checkpoint is None
                or active_checkpoint.state is not CheckpointState.READY
                or snapshot is None
            ):
                results.append(active)
                continue
            if active.owner_token != self._owner_token and _owner_is_alive(active.owner_pid):
                results.append(active)
                continue
            lock = await self._worktree_lock(active.worktree_id.value)
            async with lock:
                active_attempt = await self._checkpoints.get_attempt(active.attempt_id)
                if active_attempt is None:
                    continue
                if active_attempt.owner_token != self._owner_token:
                    try:
                        active_attempt = await self._checkpoints.compare_and_transition_attempt(
                            replace(
                                active_attempt,
                                owner_pid=os.getpid(),
                                owner_token=self._owner_token,
                                state=RollbackState.STARTED,
                            ),
                            expected_version=active_attempt.version,
                            expected_state=active_attempt.state,
                        )
                    except WorkspaceCheckpointError:
                        results.append(active_attempt)
                        continue
                try:
                    projection = await self._artifacts.load(active_checkpoint)
                    result = await self._resume_attempt(
                        active_checkpoint,
                        projection,
                        snapshot,
                        active_attempt,
                    )
                except WorkspaceCheckpointError as error:
                    latest = await self._checkpoints.get_attempt(active.attempt_id)
                    if latest is None:
                        raise error
                    result = await self._record_indeterminate(latest, error)
                results.append(result)
        return tuple(results)

    async def _capture(self, handle: WorktreeHandle) -> WorkspaceProjection:
        try:
            async with asyncio.timeout(MAX_CHECKPOINT_CAPTURE_SECONDS):
                return await self._state.inspect(handle)
        except TimeoutError as error:
            raise WorkspaceCheckpointError(
                "workspace checkpoint capture timed out",
                kind=CheckpointFailureKind.TIMEOUT,
            ) from error

    async def _get_or_start_attempt(
        self,
        checkpoint: WorkspaceCheckpoint,
        snapshot: WorktreeSnapshot,
        requested_attempt_id: RollbackAttemptId | None,
    ) -> RollbackAttempt:
        active = await self._checkpoints.active_attempt(snapshot.worktree_id.value)
        if active is not None:
            if active.checkpoint_id != checkpoint.checkpoint_id:
                raise WorkspaceCheckpointError(
                    "another rollback already owns this managed worktree",
                    kind=CheckpointFailureKind.ALREADY_ROLLING_BACK,
                )
            if requested_attempt_id is not None and requested_attempt_id != active.attempt_id:
                raise WorkspaceCheckpointError(
                    "requested rollback attempt does not own the active operation",
                    kind=CheckpointFailureKind.CONCURRENT_MODIFICATION,
                )
            if active.owner_token != self._owner_token:
                if _owner_is_alive(active.owner_pid):
                    raise WorkspaceCheckpointError(
                        "another process is still rolling back this worktree",
                        kind=CheckpointFailureKind.ALREADY_ROLLING_BACK,
                    )
                active = await self._claim_attempt(active)
            return active
        identifier = requested_attempt_id or self._attempt_id_factory()
        if not isinstance(identifier, RollbackAttemptId):
            raise TypeError("rollback attempt factory must return RollbackAttemptId")
        return await self._checkpoints.start_attempt(
            RollbackAttempt(
                attempt_id=identifier,
                checkpoint_id=checkpoint.checkpoint_id,
                worktree_id=snapshot.worktree_id,
                state=RollbackState.STARTED,
                started_at=self._clock().astimezone(UTC),
                completed_at=None,
                expected_fingerprint=checkpoint.source_fingerprint,
                owner_pid=os.getpid(),
                owner_token=self._owner_token,
            )
        )

    async def _claim_attempt(self, attempt: RollbackAttempt) -> RollbackAttempt:
        return await self._checkpoints.compare_and_transition_attempt(
            replace(
                attempt,
                owner_pid=os.getpid(),
                owner_token=self._owner_token,
                state=RollbackState.STARTED,
            ),
            expected_version=attempt.version,
            expected_state=attempt.state,
        )

    async def _resume_attempt(
        self,
        checkpoint: WorkspaceCheckpoint,
        projection: WorkspaceProjection,
        snapshot: WorktreeSnapshot,
        attempt: RollbackAttempt,
    ) -> RollbackAttempt:
        reason = f"neuro-code-checkpoint:{attempt.attempt_id.value}"
        destructive_started = False
        try:
            _, status = await self._prove_handle(snapshot.handle, allow_lock_reason=reason)
            if status.head_sha != checkpoint.head_sha:
                raise WorkspaceCheckpointError(
                    "managed worktree HEAD no longer matches checkpoint HEAD",
                    kind=CheckpointFailureKind.HEAD_MISMATCH,
                )
            if not status.locked:
                await self._workspace_git.lock_worktree(snapshot.canonical_path, reason)
                destructive_started = True
                _, status = await self._prove_handle(snapshot.handle, allow_lock_reason=reason)
            else:
                # An existing lock with this exact attempt reason means a
                # previous owner may already have entered the destructive
                # phase before dying.
                destructive_started = True
            actual = await self._state.inspect(snapshot.handle)
            actual_fingerprint = workspace_projection_fingerprint(snapshot.handle, actual)
            if actual_fingerprint != checkpoint.source_fingerprint:
                await self._state.restore(snapshot.handle, projection)
                actual = await self._state.inspect(snapshot.handle)
                actual_fingerprint = workspace_projection_fingerprint(snapshot.handle, actual)
            if actual_fingerprint != checkpoint.source_fingerprint:
                raise WorkspaceCheckpointError(
                    "rollback final workspace fingerprint does not match checkpoint",
                    kind=CheckpointFailureKind.ROLLBACK_VERIFICATION_FAILED,
                )
            _, final_status = await self._prove_handle(
                snapshot.handle,
                allow_lock_reason=reason,
            )
            if not final_status.locked:
                raise WorkspaceCheckpointError(
                    "managed worktree rollback lock disappeared before completion",
                    kind=CheckpointFailureKind.LOCKED,
                )
            await self._workspace_git.unlock_worktree(snapshot.canonical_path)
            completed = replace(
                attempt,
                state=RollbackState.COMPLETED,
                completed_at=self._clock().astimezone(UTC),
                observed_fingerprint=actual_fingerprint,
                error_kind=None,
            )
            return await self._checkpoints.compare_and_transition_attempt(
                completed,
                expected_version=attempt.version,
                expected_state=attempt.state,
            )
        except WorkspaceCheckpointError as error:
            return await self._mark_attempt_failure(attempt, error, destructive_started)
        except WorktreeError as error:
            wrapped = WorkspaceCheckpointError(
                "managed worktree rollback Git boundary failed",
                kind=(
                    CheckpointFailureKind.LOCKED
                    if "lock" in str(error).casefold()
                    else CheckpointFailureKind.COMMAND_FAILED
                ),
            )
            return await self._mark_attempt_failure(attempt, wrapped, destructive_started)

    async def _mark_attempt_failure(
        self,
        attempt: RollbackAttempt,
        error: WorkspaceCheckpointError,
        destructive_started: bool = False,
    ) -> RollbackAttempt:
        state = (
            RollbackState.FAILED
            if not destructive_started
            and error.kind
            in {
                CheckpointFailureKind.HEAD_MISMATCH,
                CheckpointFailureKind.LOCKED,
                CheckpointFailureKind.IDENTITY_MISMATCH,
                CheckpointFailureKind.UNMANAGED,
            }
            else RollbackState.INDETERMINATE
        )
        failed = replace(attempt, state=state, error_kind=str(error.kind))
        try:
            await self._checkpoints.compare_and_transition_attempt(
                failed,
                expected_version=attempt.version,
                expected_state=attempt.state,
            )
        except WorkspaceCheckpointError:
            updated = await self._checkpoints.get_attempt(attempt.attempt_id)
            if updated is None:
                raise error from None
        raise error

    async def _record_indeterminate(
        self,
        attempt: RollbackAttempt,
        error: WorkspaceCheckpointError,
    ) -> RollbackAttempt:
        """Persist uncertainty without touching a potentially protected worktree."""

        indeterminate = replace(
            attempt,
            state=RollbackState.INDETERMINATE,
            error_kind=str(error.kind),
        )
        try:
            return await self._checkpoints.compare_and_transition_attempt(
                indeterminate,
                expected_version=attempt.version,
                expected_state=attempt.state,
            )
        except WorkspaceCheckpointError:
            latest = await self._checkpoints.get_attempt(attempt.attempt_id)
            if latest is None:
                raise error from None
            return latest

    async def _prove_handle(
        self,
        handle: WorktreeHandle,
        *,
        allow_lock_reason: str | None,
    ) -> tuple[WorktreeSnapshot, WorktreeStatus]:
        snapshot = await self._worktrees.get(handle.worktree_id.value)
        if snapshot is None or snapshot.ownership is not WorktreeOwnership.MANAGED:
            raise WorkspaceCheckpointError(
                "worktree is not owned by Neuro Code",
                kind=CheckpointFailureKind.UNMANAGED,
            )
        if snapshot.state is not WorktreeState.READY or snapshot.handle != handle:
            raise WorkspaceCheckpointError(
                "worktree is not a ready identity-bound managed target",
                kind=CheckpointFailureKind.IDENTITY_MISMATCH,
            )
        try:
            repository = await self._git.repository_identity(snapshot.repository.source_worktree)
            records = await self._git.list_worktrees(snapshot.repository.source_worktree)
            status = await self._git.inspect_status(handle.path)
        except WorktreeError as error:
            raise WorkspaceCheckpointError(
                "managed worktree identity could not be proven",
                kind=CheckpointFailureKind.IDENTITY_MISMATCH,
            ) from error
        if not _same_repository(snapshot, repository):
            raise WorkspaceCheckpointError(
                "managed worktree repository identity changed",
                kind=CheckpointFailureKind.IDENTITY_MISMATCH,
            )
        record = next((item for item in records if item.path == handle.path), None)
        if record is None or status.path != handle.path:
            raise WorkspaceCheckpointError(
                "managed worktree is missing or has been replaced",
                kind=CheckpointFailureKind.IDENTITY_MISMATCH,
            )
        expected_branch = None if handle.branch is None else f"refs/heads/{handle.branch}"
        if record.branch != expected_branch or record.detached is not (handle.branch is None):
            raise WorkspaceCheckpointError(
                "managed worktree branch or detached identity changed",
                kind=CheckpointFailureKind.IDENTITY_MISMATCH,
            )
        if record.locked:
            reason = getattr(record, "lock_reason", None)
            if allow_lock_reason is None or reason != allow_lock_reason:
                raise WorkspaceCheckpointError(
                    "managed worktree is locked by an external owner",
                    kind=CheckpointFailureKind.LOCKED,
                )
        return snapshot, status

    async def _worktree_lock(self, worktree_id: str) -> asyncio.Lock:
        async with self._worktree_locks_guard:
            return self._worktree_locks.setdefault(worktree_id, asyncio.Lock())

    def _require_initialized(self) -> None:
        if not self._initialized:
            raise WorkspaceCheckpointError(
                "workspace checkpoint application service is not initialized",
                kind=CheckpointFailureKind.FAILED_STATE,
            )


__all__ = ["WorkspaceCheckpointApplicationService"]
