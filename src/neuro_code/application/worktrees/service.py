"""Application service for isolated, locally managed Git worktrees.

The service owns lifecycle intent and ownership proof.  It never treats a
directory name as authority, never copies dirty source state, and never uses
force removal.  Git and SQLite are reconciled explicitly after uncertain
boundaries rather than presented as one atomic transaction.

隔离且由本地应用管理的 Git worktree 应用服务.

该服务拥有生命周期 intent 和 ownership proof.它从不把目录名当作 authority,不复制
源工作区 dirty state,也不使用强制删除.在不确定边界后显式 reconciliation Git 与
SQLite,而不是把二者伪装成一个原子事务.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from neuro_code.application.ports.worktree import (
    MINIMUM_GIT_VERSION,
    GitWorktreePort,
    GitWorktreeRecord,
    ManagedWorktreeStore,
    WorktreeError,
    WorktreeFailureKind,
)
from neuro_code.domain.worktree import (
    WorktreeCreateRequest,
    WorktreeHandle,
    WorktreeId,
    WorktreeKind,
    WorktreeOwnership,
    WorktreeRemoveRequest,
    WorktreeRepositoryIdentity,
    WorktreeSnapshot,
    WorktreeState,
    WorktreeStatus,
    WorktreeWorkspaceBinding,
)
from neuro_code.shared.async_utils import run_blocking

Clock = Callable[[], datetime]
WorktreeIdFactory = Callable[[], WorktreeId]


def _now() -> datetime:
    return datetime.now(UTC)


def _paths_overlap(first: Path, second: Path) -> bool:
    return first == second or first.is_relative_to(second) or second.is_relative_to(first)


def _record_matches_snapshot(record: GitWorktreeRecord, snapshot: WorktreeSnapshot) -> bool:
    expected_branch = None if snapshot.branch is None else f"refs/heads/{snapshot.branch}"
    return (
        record.path == snapshot.canonical_path
        and record.head_sha == snapshot.base_commit_sha
        and record.detached is (snapshot.kind is WorktreeKind.DETACHED)
        and record.branch == expected_branch
    )


def _repository_identity_matches(
    repository: WorktreeRepositoryIdentity,
    snapshot: WorktreeSnapshot,
) -> bool:
    """Compare stable repository facts, excluding mutable source HEAD."""

    return (
        repository.common_dir == snapshot.repository.common_dir
        and repository.source_worktree == snapshot.repository.source_worktree
        and repository.git_dir == snapshot.repository.git_dir
    )


def _status_from_record(record: GitWorktreeRecord) -> WorktreeStatus:
    branch = record.branch
    if branch is not None and branch.startswith("refs/heads/"):
        branch = branch.removeprefix("refs/heads/")
    return WorktreeStatus(
        path=record.path,
        head_sha=record.head_sha,
        branch=branch,
        detached=record.detached,
        locked=record.locked,
        prunable=record.prunable,
    )


class WorktreeApplicationService:
    """Create, inspect, reconcile, and safely remove owned worktrees."""

    def __init__(
        self,
        *,
        git: GitWorktreePort,
        store: ManagedWorktreeStore,
        managed_root: Path,
        clock: Clock = _now,
        id_factory: WorktreeIdFactory = WorktreeId.new,
    ) -> None:
        self._git = git
        self._store = store
        self._managed_root = managed_root.expanduser().resolve(strict=False)
        self._clock = clock
        self._id_factory = id_factory
        self._initialized = False
        self._repository_locks: dict[str, asyncio.Lock] = {}
        self._repository_locks_guard = asyncio.Lock()
        if self._managed_root == self._managed_root.parent:
            raise ValueError("managed worktree root must not be the filesystem root")

    @property
    def managed_root(self) -> Path:
        return self._managed_root

    async def initialize(self) -> None:
        """Initialize durable ownership and verify the local Git capability."""

        await self._store.initialize()
        version = await self._git.git_version()
        if version < MINIMUM_GIT_VERSION:
            raise WorktreeError(
                "installed Git must be >= 2.40.0 for managed worktree lifecycle operations",
                kind=WorktreeFailureKind.NOT_AVAILABLE,
            )
        self._initialized = True

    async def create(self, request: WorktreeCreateRequest) -> WorktreeSnapshot:
        self._require_initialized()
        if not isinstance(request, WorktreeCreateRequest):
            raise TypeError("worktree create accepts a canonical request")
        repository = await self._git.repository_identity(request.repository_path)
        lock = await self._repository_lock(repository.repository_id)
        async with lock:
            return await self._create_locked(request, repository)

    async def _create_locked(
        self,
        request: WorktreeCreateRequest,
        repository: WorktreeRepositoryIdentity,
    ) -> WorktreeSnapshot:
        if _paths_overlap(self._managed_root, repository.source_worktree):
            raise WorktreeError(
                "managed worktree root must be outside the source checkout",
                kind=WorktreeFailureKind.PATH_CONFLICT,
            )
        worktree_id = request.worktree_id or self._id_factory()
        if not isinstance(worktree_id, WorktreeId):
            raise TypeError("worktree id factory must return WorktreeId")
        existing = await self._store.get(worktree_id.value)
        if existing is not None:
            raise WorktreeError(
                "worktree id is already owned or was used previously",
                kind=WorktreeFailureKind.PATH_CONFLICT,
            )
        target = self._managed_path(repository, worktree_id)
        if target.exists() or target.is_symlink():
            raise WorktreeError(
                "managed worktree target path already exists",
                kind=WorktreeFailureKind.PATH_CONFLICT,
            )
        base_commit_sha = await self._git.resolve_commit(
            repository.source_worktree,
            request.base_revision,
        )
        branch: str | None = None
        if request.kind is WorktreeKind.MANAGED_BRANCH:
            branch = await self._git.validate_branch(
                repository.source_worktree,
                request.branch or f"neuro/worktree/{worktree_id.value}",
            )
            if await self._git.branch_exists(repository.source_worktree, branch):
                raise WorktreeError(
                    "managed worktree branch already exists",
                    kind=WorktreeFailureKind.BRANCH_CONFLICT,
                )
        elif request.branch is not None:
            raise WorktreeError(
                "detached worktree cannot use a branch",
                kind=WorktreeFailureKind.INVALID_REF,
            )
        await self._git.preflight_checkout(repository.source_worktree, base_commit_sha)

        intent = WorktreeSnapshot(
            worktree_id=worktree_id,
            repository=repository,
            canonical_path=target,
            base_revision=request.base_revision,
            base_commit_sha=base_commit_sha,
            branch=branch,
            kind=request.kind,
            ownership=WorktreeOwnership.MANAGED,
            state=WorktreeState.CREATING,
            created_at=self._clock().astimezone(UTC),
            created_by_session_id=request.created_by_session_id,
        )
        intent = await self._store.insert_intent(intent)
        try:
            await self._git.add_worktree(
                repository.source_worktree,
                target,
                base_commit_sha,
                branch=branch,
            )
        except WorktreeError:
            await self._transition(intent, replace(intent, state=WorktreeState.FAILED))
            raise

        try:
            actual = await self._actual_record(repository, target)
            if actual is None or not _record_matches_snapshot(actual, intent):
                orphaned = replace(intent, state=WorktreeState.ORPHANED)
                await self._transition(intent, orphaned)
                raise WorktreeError(
                    "created worktree identity does not match durable intent",
                    kind=WorktreeFailureKind.IDENTITY_MISMATCH,
                )
            status = await self._git.inspect_status(target)
            if status.dirty:
                orphaned = replace(intent, state=WorktreeState.ORPHANED, status=status)
                await self._transition(intent, orphaned)
                raise WorktreeError(
                    "new worktree is unexpectedly dirty",
                    kind=WorktreeFailureKind.IDENTITY_MISMATCH,
                )
        except WorktreeError:
            # Do not force-delete an uncertain result.  The durable record is
            # intentionally left for explicit reconciliation/inspection.
            current = await self._store.get(worktree_id.value)
            if current is not None and current.state is WorktreeState.CREATING:
                await self._transition(current, replace(current, state=WorktreeState.FAILED))
            raise
        ready = replace(intent, state=WorktreeState.READY, status=status)
        return await self._transition(intent, ready)

    async def list_managed(self, *, reconcile: bool = True) -> tuple[WorktreeSnapshot, ...]:
        self._require_initialized()
        if reconcile:
            await self.reconcile_managed_worktrees()
        return await self._store.list()

    async def inspect(self, worktree_id: str, /) -> WorktreeSnapshot:
        self._require_initialized()
        identifier = WorktreeId(worktree_id)
        await self.reconcile_managed_worktrees(worktree_id=identifier)
        snapshot = await self._store.get(identifier.value)
        if snapshot is None:
            raise WorktreeError(
                "worktree is not owned by Neuro Code", kind=WorktreeFailureKind.UNMANAGED
            )
        return snapshot

    async def status(self, worktree_id: str, /) -> WorktreeStatus:
        snapshot = await self.inspect(worktree_id)
        if snapshot.status is not None:
            return snapshot.status
        if snapshot.state is not WorktreeState.READY:
            raise WorktreeError(
                "worktree is not ready for status inspection",
                kind=WorktreeFailureKind.FAILED_STATE,
            )
        status = await self._git.inspect_status(snapshot.canonical_path)
        await self._transition(snapshot, replace(snapshot, status=status))
        return status

    async def workspace_binding(self, worktree_id: str, /) -> WorktreeWorkspaceBinding:
        snapshot = await self.inspect(worktree_id)
        if snapshot.state is not WorktreeState.READY:
            raise WorktreeError(
                "only a ready managed worktree can become a workspace binding",
                kind=WorktreeFailureKind.FAILED_STATE,
            )
        if not snapshot.canonical_path.is_dir():
            raise WorktreeError(
                "managed worktree path is not an existing directory",
                kind=WorktreeFailureKind.IDENTITY_MISMATCH,
            )
        # Additional roots are deliberately empty; callers must delegate any
        # extra authority explicitly in a future capability.
        return WorktreeWorkspaceBinding(primary_root=snapshot.canonical_path)

    async def remove(self, request: WorktreeRemoveRequest) -> WorktreeSnapshot:
        self._require_initialized()
        if not isinstance(request, WorktreeRemoveRequest):
            raise TypeError("worktree remove accepts a canonical request")
        snapshot = await self._store.get(request.worktree_id.value)
        if snapshot is None or snapshot.ownership is not WorktreeOwnership.MANAGED:
            raise WorktreeError(
                "worktree is not owned by Neuro Code", kind=WorktreeFailureKind.UNMANAGED
            )
        lock = await self._repository_lock(snapshot.repository.repository_id)
        async with lock:
            return await self._remove_locked(request.worktree_id)

    async def _remove_locked(self, worktree_id: WorktreeId) -> WorktreeSnapshot:
        snapshot = await self._store.get(worktree_id.value)
        if snapshot is None or snapshot.ownership is not WorktreeOwnership.MANAGED:
            raise WorktreeError(
                "worktree is not owned by Neuro Code", kind=WorktreeFailureKind.UNMANAGED
            )
        if snapshot.state is WorktreeState.REMOVED:
            return snapshot
        if snapshot.state is not WorktreeState.READY:
            raise WorktreeError(
                "worktree is not in a removable lifecycle state",
                kind=WorktreeFailureKind.FAILED_STATE,
            )
        repository = await self._git.repository_identity(snapshot.repository.source_worktree)
        if not _repository_identity_matches(repository, snapshot):
            await self._transition(snapshot, replace(snapshot, state=WorktreeState.ORPHANED))
            raise WorktreeError(
                "repository identity no longer matches ownership record",
                kind=WorktreeFailureKind.IDENTITY_MISMATCH,
            )
        actual = await self._actual_record(repository, snapshot.canonical_path)
        if actual is None or not _record_matches_snapshot(actual, snapshot):
            orphaned = replace(snapshot, state=WorktreeState.ORPHANED)
            await self._transition(snapshot, orphaned)
            raise WorktreeError(
                "worktree identity cannot be proven for removal",
                kind=WorktreeFailureKind.IDENTITY_MISMATCH,
            )
        status = await self._git.inspect_status(snapshot.canonical_path)
        if status.locked:
            await self._transition(snapshot, replace(snapshot, status=status))
            raise WorktreeError(
                "locked worktree cannot be removed automatically", kind=WorktreeFailureKind.LOCKED
            )
        if status.dirty:
            await self._transition(snapshot, replace(snapshot, status=status))
            raise WorktreeError(
                "dirty worktree refuses non-force removal", kind=WorktreeFailureKind.DIRTY
            )
        removing = replace(snapshot, state=WorktreeState.REMOVING, status=status)
        removing = await self._transition(snapshot, removing)
        try:
            await self._git.remove_worktree(repository.source_worktree, snapshot.canonical_path)
        except WorktreeError:
            await self._transition(removing, replace(removing, state=WorktreeState.FAILED))
            raise
        records = await self._git.list_worktrees(repository.source_worktree)
        if any(record.path == snapshot.canonical_path for record in records):
            orphaned = replace(removing, state=WorktreeState.ORPHANED)
            await self._transition(removing, orphaned)
            raise WorktreeError(
                "Git still reports the worktree after removal",
                kind=WorktreeFailureKind.IDENTITY_MISMATCH,
            )
        removed = replace(removing, state=WorktreeState.REMOVED, status=None)
        return await self._transition(removing, removed)

    async def reconcile_managed_worktrees(
        self,
        *,
        worktree_id: WorktreeId | None = None,
    ) -> tuple[WorktreeSnapshot, ...]:
        self._require_initialized()
        snapshots = await self._store.list(include_removed=False)
        if worktree_id is not None:
            snapshots = tuple(
                snapshot for snapshot in snapshots if snapshot.worktree_id == worktree_id
            )
        reconciled: list[WorktreeSnapshot] = []
        for snapshot in snapshots:
            lock = await self._repository_lock(snapshot.repository.repository_id)
            async with lock:
                current = await self._store.get(snapshot.worktree_id.value)
                if current is None or current.state is WorktreeState.REMOVED:
                    continue
                updated = await self._reconcile_one(current)
                if updated != current:
                    try:
                        updated = await self._transition(current, updated)
                    except WorktreeError as error:
                        if error.kind is not WorktreeFailureKind.CONCURRENT_MODIFICATION:
                            raise
                        latest = await self._store.get(current.worktree_id.value)
                        if latest is None or latest.state is WorktreeState.REMOVED:
                            continue
                        updated = latest
                reconciled.append(updated)
        return tuple(reconciled)

    async def _reconcile_one(self, snapshot: WorktreeSnapshot) -> WorktreeSnapshot:
        try:
            repository = await self._git.repository_identity(snapshot.repository.source_worktree)
        except WorktreeError:
            return replace(snapshot, state=WorktreeState.ORPHANED, status=None)
        if not _repository_identity_matches(repository, snapshot):
            return replace(snapshot, state=WorktreeState.ORPHANED, status=None)
        try:
            records = await self._git.list_worktrees(repository.source_worktree)
        except WorktreeError:
            return replace(snapshot, state=WorktreeState.FAILED)
        actual = next(
            (record for record in records if record.path == snapshot.canonical_path), None
        )
        if actual is None:
            path_reused = await run_blocking(
                lambda: snapshot.canonical_path.exists() or snapshot.canonical_path.is_symlink()
            )
            if path_reused:
                return replace(snapshot, state=WorktreeState.ORPHANED, status=None)
            state = (
                WorktreeState.REMOVED
                if snapshot.state is WorktreeState.REMOVING
                else WorktreeState.FAILED
                if snapshot.state in {WorktreeState.CREATING, WorktreeState.FAILED}
                else WorktreeState.ORPHANED
            )
            return replace(snapshot, state=state, status=None)
        if not _record_matches_snapshot(actual, snapshot):
            return replace(
                snapshot, state=WorktreeState.ORPHANED, status=_status_from_record(actual)
            )
        try:
            status = await self._git.inspect_status(snapshot.canonical_path)
        except WorktreeError:
            return replace(snapshot, state=WorktreeState.FAILED, status=_status_from_record(actual))
        return replace(snapshot, state=WorktreeState.READY, status=status)

    async def get_handle(self, worktree_id: str, /) -> WorktreeHandle:
        snapshot = await self.inspect(worktree_id)
        if snapshot.state is not WorktreeState.READY:
            raise WorktreeError(
                "only a ready managed worktree can be leased",
                kind=WorktreeFailureKind.FAILED_STATE,
            )
        return snapshot.handle

    async def _actual_record(
        self,
        repository: WorktreeRepositoryIdentity,
        target: Path,
    ) -> GitWorktreeRecord | None:
        records = await self._git.list_worktrees(repository.source_worktree)
        canonical = await run_blocking(lambda: target.expanduser().resolve(strict=False))
        return next((record for record in records if record.path == canonical), None)

    async def _transition(
        self,
        current: WorktreeSnapshot,
        proposed: WorktreeSnapshot,
    ) -> WorktreeSnapshot:
        return await self._store.compare_and_transition(
            proposed,
            expected_version=current.version,
            expected_state=current.state,
        )

    def _managed_path(
        self,
        repository: WorktreeRepositoryIdentity,
        worktree_id: WorktreeId,
    ) -> Path:
        try:
            self._managed_root.mkdir(parents=True, exist_ok=True)
            if self._managed_root.is_symlink() or not self._managed_root.is_dir():
                raise OSError("managed root is not a regular directory")
            repository_root = self._managed_root / repository.repository_id
            if repository_root.exists() and repository_root.is_symlink():
                raise OSError("managed repository root is a symlink")
            repository_root.mkdir(parents=True, exist_ok=True)
            target = (repository_root / worktree_id.value).resolve(strict=False)
            if target.parent != repository_root.resolve(strict=False):
                raise OSError("managed worktree path escaped its owner root")
            return target
        except (OSError, RuntimeError) as error:
            raise WorktreeError(
                "managed worktree root is unavailable or unsafe",
                kind=WorktreeFailureKind.PATH_CONFLICT,
            ) from error

    async def _repository_lock(self, repository_id: str) -> asyncio.Lock:
        async with self._repository_locks_guard:
            return self._repository_locks.setdefault(repository_id, asyncio.Lock())

    def _require_initialized(self) -> None:
        if not self._initialized:
            raise WorktreeError(
                "worktree application service is not initialized",
                kind=WorktreeFailureKind.FAILED_STATE,
            )


__all__ = ["WorktreeApplicationService"]
