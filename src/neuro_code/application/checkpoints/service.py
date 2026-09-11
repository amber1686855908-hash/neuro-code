"""Application service for bounded managed-workspace checkpoint/rollback."""

from __future__ import annotations

import asyncio
import ctypes as _ctypes
import os
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

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
from neuro_code.application.runtime import process_liveness
from neuro_code.domain.checkpoints import (
    CheckpointCreateRequest,
    CheckpointFingerprint,
    CheckpointId,
    CheckpointState,
    CheckpointTarget,
    RollbackAttempt,
    RollbackAttemptId,
    RollbackState,
    SourceWorkspaceCheckpointGrant,
    WorkspaceCheckpoint,
    WorkspaceProjection,
    workspace_projection_fingerprint,
)
from neuro_code.domain.worktree import (
    WorktreeHandle,
    WorktreeId,
    WorktreeOwnership,
    WorktreeRepositoryIdentity,
    WorktreeSnapshot,
    WorktreeState,
    WorktreeStatus,
)

Clock = Callable[[], datetime]


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


def _same_repository_identity(
    first: WorktreeRepositoryIdentity,
    second: WorktreeRepositoryIdentity,
) -> bool:
    return (
        first.common_dir == second.common_dir
        and first.source_worktree == second.source_worktree
        and first.git_dir == second.git_dir
    )


def _resolve_path(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


# Keep these module-level names as a compatibility seam for the existing
# Windows checkpoint probe tests while the implementation is shared with
# writable-subagent reconciliation.
ctypes = _ctypes
_ERROR_FILE_NOT_FOUND = process_liveness._ERROR_FILE_NOT_FOUND
_ERROR_INVALID_PARAMETER = process_liveness._ERROR_INVALID_PARAMETER
_WAIT_OBJECT_0 = process_liveness._WAIT_OBJECT_0
_WAIT_TIMEOUT = process_liveness._WAIT_TIMEOUT
_owner_is_alive = process_liveness.owner_is_alive


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
                "installed Git must be >= 2.40.0 for managed workspace checkpoint operations",
                kind=CheckpointFailureKind.NOT_AVAILABLE,
            )
        self._initialized = True

    async def create(self, request: CheckpointCreateRequest) -> WorkspaceCheckpoint:
        self._require_initialized()
        if not isinstance(request, CheckpointCreateRequest):
            raise TypeError("workspace checkpoint create accepts a canonical request")
        target = request.worktree
        lock = await self._worktree_lock(target.worktree_id.value)
        async with lock:
            repository, _ = await self._prove_target(target, allow_lock_reason=None)
            projection = await self._capture(target)
            fingerprint = workspace_projection_fingerprint(target, projection)
            checkpoint_id = request.checkpoint_id or self._checkpoint_id_factory()
            if not isinstance(checkpoint_id, CheckpointId):
                raise TypeError("checkpoint id factory must return CheckpointId")
            intent = WorkspaceCheckpoint(
                checkpoint_id=checkpoint_id,
                worktree_id=target.worktree_id,
                repository=repository,
                canonical_path=target.path,
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

    async def inspect(self, handle: CheckpointTarget, /) -> WorkspaceProjection:
        """Inspect one owned worktree without creating or restoring a checkpoint."""

        self._require_initialized()
        if not isinstance(handle, (WorktreeHandle, SourceWorkspaceCheckpointGrant)):
            raise TypeError("workspace inspection requires a canonical workspace grant")
        lock = await self._worktree_lock(handle.worktree_id.value)
        async with lock:
            _, status = await self._prove_target(handle, allow_lock_reason=None)
            if status.locked:
                raise WorkspaceCheckpointError(
                    "locked managed worktree cannot be inspected for a writable result",
                    kind=CheckpointFailureKind.LOCKED,
                )
            return await self._capture(handle)

    async def authorize_source_workspace(
        self,
        path: Path,
        /,
    ) -> SourceWorkspaceCheckpointGrant:
        """Issue a source-checkout grant from current Git identity facts.

        ``path`` is only a bootstrap candidate.  The returned grant is the
        authority consumed by checkpoint operations, and every operation
        proves it again against Git before touching the workspace.
        """

        self._require_initialized()
        if not isinstance(path, Path):
            raise TypeError("source workspace candidate must be a pathlib.Path")
        try:
            canonical_path = await asyncio.to_thread(_resolve_path, path)
            repository = await self._git.repository_identity(canonical_path)
            records = await self._git.list_worktrees(canonical_path)
            status = await self._git.inspect_status(canonical_path)
        except (OSError, RuntimeError, WorktreeError) as error:
            raise WorkspaceCheckpointError(
                "source checkout Git identity could not be proven",
                kind=CheckpointFailureKind.IDENTITY_MISMATCH,
            ) from error
        if repository.source_worktree != canonical_path or status.path != canonical_path:
            raise WorkspaceCheckpointError(
                "checkpoint target must be the repository source checkout",
                kind=CheckpointFailureKind.IDENTITY_MISMATCH,
            )
        record = next((item for item in records if item.path == canonical_path), None)
        if record is None or record.head_sha != status.head_sha:
            raise WorkspaceCheckpointError(
                "source checkout is not a current Git worktree",
                kind=CheckpointFailureKind.IDENTITY_MISMATCH,
            )
        if status.locked or record.locked:
            raise WorkspaceCheckpointError(
                "locked source checkout cannot be protected safely",
                kind=CheckpointFailureKind.LOCKED,
            )
        return SourceWorkspaceCheckpointGrant.issue(
            repository,
            path=canonical_path,
            head_sha=status.head_sha,
            branch=status.branch,
            detached=status.detached,
        )

    async def ignored_source_paths(
        self,
        grant: SourceWorkspaceCheckpointGrant,
        paths: tuple[str, ...],
        /,
    ) -> tuple[str, ...]:
        """Ask Git whether prepared source targets are ignored.

        The Git adapter owns ignore-file interpretation.  Tracked paths are
        excluded by that adapter so a matching ignore rule cannot weaken the
        tracked projection guarantee.
        """

        self._require_initialized()
        if not isinstance(grant, SourceWorkspaceCheckpointGrant):
            raise TypeError("ignored-path checks require a source workspace grant")
        if not isinstance(paths, tuple):
            raise TypeError("ignored source paths must be a tuple")
        await self._prove_source_grant(grant, allow_lock_reason=None)
        try:
            return await self._workspace_git.ignored_paths(grant.path, paths)
        except AttributeError as error:
            raise WorkspaceCheckpointError(
                "Git ignored-path capability is unavailable",
                kind=CheckpointFailureKind.NOT_AVAILABLE,
            ) from error

    async def get(self, checkpoint_id: CheckpointId, /) -> WorkspaceCheckpoint | None:
        """Read one checkpoint metadata record for internal reconciliation."""

        self._require_initialized()
        if not isinstance(checkpoint_id, CheckpointId):
            raise TypeError("checkpoint id must be canonical")
        return await self._checkpoints.get(checkpoint_id)

    async def load_projection(self, checkpoint_id: CheckpointId, /) -> WorkspaceProjection:
        """Load and integrity-check the exact READY checkpoint projection."""

        self._require_initialized()
        if not isinstance(checkpoint_id, CheckpointId):
            raise TypeError("checkpoint id must be canonical")
        checkpoint = await self._checkpoints.get(checkpoint_id)
        if checkpoint is None or checkpoint.state is not CheckpointState.READY:
            raise WorkspaceCheckpointError(
                "checkpoint is not a ready Neuro Code-owned target",
                kind=CheckpointFailureKind.UNMANAGED,
            )
        try:
            return await self._artifacts.load(checkpoint)
        except WorkspaceCheckpointError:
            raise
        except BaseException as error:
            raise WorkspaceCheckpointError(
                "checkpoint projection could not be loaded safely",
                kind=CheckpointFailureKind.CHECKPOINT_CORRUPT,
            ) from error

    async def rollback(
        self,
        checkpoint_id: CheckpointId,
        *,
        target: CheckpointTarget | None = None,
        attempt_id: RollbackAttemptId | None = None,
        expected_current_fingerprint: CheckpointFingerprint | None = None,
    ) -> RollbackAttempt:
        self._require_initialized()
        if not isinstance(checkpoint_id, CheckpointId):
            raise TypeError("checkpoint id must be canonical")
        if attempt_id is not None and not isinstance(attempt_id, RollbackAttemptId):
            raise TypeError("rollback attempt id must be canonical")
        if expected_current_fingerprint is not None and not isinstance(
            expected_current_fingerprint,
            CheckpointFingerprint,
        ):
            raise TypeError("expected current fingerprint must be canonical")
        if target is not None and not isinstance(
            target, (WorktreeHandle, SourceWorkspaceCheckpointGrant)
        ):
            raise TypeError("rollback target must be a canonical workspace grant")
        checkpoint = await self._checkpoints.get(checkpoint_id)
        if checkpoint is None or checkpoint.state is not CheckpointState.READY:
            raise WorkspaceCheckpointError(
                "checkpoint is not a ready Neuro Code-owned target",
                kind=CheckpointFailureKind.UNMANAGED,
            )
        projection = await self._artifacts.load(checkpoint)
        snapshot: WorktreeSnapshot | None = None
        if target is None:
            snapshot = await self._worktrees.get(checkpoint.worktree_id.value)
            if snapshot is None:
                raise WorkspaceCheckpointError(
                    "checkpoint worktree ownership record is missing",
                    kind=CheckpointFailureKind.UNMANAGED,
                )
            target = snapshot.handle
        elif target.worktree_id != checkpoint.worktree_id:
            raise WorkspaceCheckpointError(
                "rollback target does not match the checkpoint identity",
                kind=CheckpointFailureKind.IDENTITY_MISMATCH,
            )
        lock = await self._worktree_lock(target.worktree_id.value)
        async with lock:
            _, status = await self._prove_target(target, allow_lock_reason=None)
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
                target.worktree_id,
                attempt_id,
            )
            if (
                attempt.state is RollbackState.COMPLETED
                and attempt.observed_fingerprint != checkpoint.source_fingerprint
            ):
                raise WorkspaceCheckpointError(
                    "completed rollback does not prove the checkpoint projection",
                    kind=CheckpointFailureKind.ROLLBACK_VERIFICATION_FAILED,
                )
            if attempt.state is RollbackState.COMPLETED:
                return attempt
            return await self._resume_attempt(
                checkpoint,
                projection,
                target,
                attempt,
                expected_current_fingerprint=expected_current_fingerprint,
            )

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
            source_target: SourceWorkspaceCheckpointGrant | None = None
            if snapshot is None and active_checkpoint is not None:
                try:
                    source_target = SourceWorkspaceCheckpointGrant.from_checkpoint(
                        active_checkpoint
                    )
                except (TypeError, ValueError):
                    source_target = None
            if (
                active_checkpoint is None
                or active_checkpoint.state is not CheckpointState.READY
                or (snapshot is None and source_target is None)
            ):
                results.append(active)
                continue
            if active.owner_token != self._owner_token and _owner_is_alive(active.owner_pid):
                results.append(active)
                continue
            if snapshot is not None:
                target: CheckpointTarget = snapshot.handle
            elif source_target is not None:
                target = source_target
            else:
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
                        target,
                        active_attempt,
                    )
                except WorkspaceCheckpointError as error:
                    latest = await self._checkpoints.get_attempt(active.attempt_id)
                    if latest is None:
                        raise error
                    result = await self._record_indeterminate(latest, error)
                results.append(result)
        return tuple(results)

    async def _capture(self, handle: CheckpointTarget) -> WorkspaceProjection:
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
        worktree_id: WorktreeId,
        requested_attempt_id: RollbackAttemptId | None,
    ) -> RollbackAttempt:
        active = await self._checkpoints.active_attempt(worktree_id.value)
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
        if requested_attempt_id is not None:
            existing = await self._checkpoints.get_attempt(requested_attempt_id)
            if existing is not None:
                if existing.checkpoint_id != checkpoint.checkpoint_id:
                    raise WorkspaceCheckpointError(
                        "requested rollback attempt belongs to another checkpoint",
                        kind=CheckpointFailureKind.CONCURRENT_MODIFICATION,
                    )
                if existing.state is RollbackState.COMPLETED:
                    return existing
                raise WorkspaceCheckpointError(
                    "requested rollback attempt is no longer resumable",
                    kind=CheckpointFailureKind.CONCURRENT_MODIFICATION,
                )
        return await self._checkpoints.start_attempt(
            RollbackAttempt(
                attempt_id=identifier,
                checkpoint_id=checkpoint.checkpoint_id,
                worktree_id=worktree_id,
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
        target: CheckpointTarget,
        attempt: RollbackAttempt,
        *,
        expected_current_fingerprint: CheckpointFingerprint | None = None,
    ) -> RollbackAttempt:
        reason = f"neuro-code-checkpoint:{attempt.attempt_id.value}"
        destructive_started = False
        try:
            _, status = await self._prove_target(target, allow_lock_reason=reason)
            if status.head_sha != checkpoint.head_sha:
                raise WorkspaceCheckpointError(
                    "managed worktree HEAD no longer matches checkpoint HEAD",
                    kind=CheckpointFailureKind.HEAD_MISMATCH,
                )
            if not status.locked and isinstance(target, WorktreeHandle):
                await self._workspace_git.lock_worktree(target.path, reason)
                destructive_started = True
                _, status = await self._prove_target(target, allow_lock_reason=reason)
            elif status.locked:
                # An existing lock with this exact attempt reason means a
                # previous owner may already have entered the destructive
                # phase before dying.
                destructive_started = True
            if isinstance(target, SourceWorkspaceCheckpointGrant):
                # Source checkouts have no managed-worktree lock.  Once their
                # identity proof has passed, any later inspection or restore
                # failure must be treated conservatively as potentially
                # destructive during reconciliation.
                destructive_started = True
            actual = await self._state.inspect(target)
            actual_fingerprint = workspace_projection_fingerprint(target, actual)
            if (
                expected_current_fingerprint is not None
                and actual_fingerprint != expected_current_fingerprint
            ):
                raise WorkspaceCheckpointError(
                    "workspace changed after rollback was claimed",
                    kind=CheckpointFailureKind.CONCURRENT_MODIFICATION,
                )
            if actual_fingerprint != checkpoint.source_fingerprint:
                await self._state.restore(target, projection)
                actual = await self._state.inspect(target)
                actual_fingerprint = workspace_projection_fingerprint(target, actual)
            if actual_fingerprint != checkpoint.source_fingerprint:
                raise WorkspaceCheckpointError(
                    "rollback final workspace fingerprint does not match checkpoint",
                    kind=CheckpointFailureKind.ROLLBACK_VERIFICATION_FAILED,
                )
            _, final_status = await self._prove_target(target, allow_lock_reason=reason)
            if isinstance(target, WorktreeHandle):
                if not final_status.locked:
                    raise WorkspaceCheckpointError(
                        "managed worktree rollback lock disappeared before completion",
                        kind=CheckpointFailureKind.LOCKED,
                    )
                await self._workspace_git.unlock_worktree(target.path)
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

    async def _prove_target(
        self,
        target: CheckpointTarget,
        *,
        allow_lock_reason: str | None,
    ) -> tuple[WorktreeRepositoryIdentity, WorktreeStatus]:
        if isinstance(target, WorktreeHandle):
            snapshot, status = await self._prove_handle(
                target,
                allow_lock_reason=allow_lock_reason,
            )
            return snapshot.repository, status
        if isinstance(target, SourceWorkspaceCheckpointGrant):
            return await self._prove_source_grant(
                target,
                allow_lock_reason=allow_lock_reason,
            )
        raise TypeError("checkpoint target must be a canonical workspace grant")

    async def _prove_source_grant(
        self,
        grant: SourceWorkspaceCheckpointGrant,
        *,
        allow_lock_reason: str | None,
    ) -> tuple[WorktreeRepositoryIdentity, WorktreeStatus]:
        del allow_lock_reason
        try:
            repository = await self._git.repository_identity(grant.path)
            records = await self._git.list_worktrees(grant.path)
            status = await self._git.inspect_status(grant.path)
        except WorktreeError as error:
            raise WorkspaceCheckpointError(
                "source checkout identity could not be proven",
                kind=CheckpointFailureKind.IDENTITY_MISMATCH,
            ) from error
        if (
            grant.path != grant.repository.source_worktree
            or repository.source_worktree != grant.path
            or not _same_repository_identity(grant.repository, repository)
            or status.path != grant.path
        ):
            raise WorkspaceCheckpointError(
                "source checkout repository identity changed",
                kind=CheckpointFailureKind.IDENTITY_MISMATCH,
            )
        record = next((item for item in records if item.path == grant.path), None)
        if record is None or record.head_sha != status.head_sha:
            raise WorkspaceCheckpointError(
                "source checkout is missing from the Git worktree boundary",
                kind=CheckpointFailureKind.IDENTITY_MISMATCH,
            )
        if (
            status.head_sha != grant.head_sha
            or status.branch != grant.branch
            or status.detached is not grant.detached
            or record.branch != (None if grant.detached else f"refs/heads/{grant.branch}")
            or record.detached is not grant.detached
        ):
            raise WorkspaceCheckpointError(
                "source checkout HEAD or branch identity changed",
                kind=CheckpointFailureKind.HEAD_MISMATCH,
            )
        if status.locked or record.locked:
            raise WorkspaceCheckpointError(
                "source checkout is locked by an external owner",
                kind=CheckpointFailureKind.LOCKED,
            )
        return repository, status

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
