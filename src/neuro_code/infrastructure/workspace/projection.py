"""Parent-workspace projection adapter for durable result adoption.

The parent root is supplied by the active conversation binding.  This adapter
only turns that trusted root into the same bounded Git/index projection used by
managed checkpoints; it does not accept worker paths or model-provided paths.
"""

from __future__ import annotations

from pathlib import Path

from neuro_code.application.ports.checkpoints import (
    CheckpointFailureKind,
    WorkspaceCheckpointError,
    WorkspaceStatePort,
)
from neuro_code.application.ports.result_adoption import (
    ParentWorkspaceProjectionReader,
    ParentWorkspaceSnapshot,
)
from neuro_code.application.ports.worktree import GitWorktreePort
from neuro_code.domain.worktree import WorktreeHandle, WorktreeId
from neuro_code.shared.async_utils import run_blocking


class LocalParentWorkspaceProjectionReader(ParentWorkspaceProjectionReader):
    """Read the active parent checkout through canonical local adapters."""

    def __init__(self, *, git: GitWorktreePort, state: WorkspaceStatePort) -> None:
        self._git = git
        self._state = state

    async def inspect(self, root: Path, /) -> ParentWorkspaceSnapshot:
        repository = await self._git.repository_identity(root)
        resolved_root = await run_blocking(lambda: root.expanduser().resolve(strict=False))
        if repository.source_worktree != resolved_root:
            raise WorkspaceCheckpointError(
                "result adoption parent must be the repository source checkout",
                kind=CheckpointFailureKind.IDENTITY_MISMATCH,
            )
        handle = WorktreeHandle(
            worktree_id=WorktreeId(f"parent-{repository.repository_id}"),
            repository=repository,
            path=repository.source_worktree,
            base_commit_sha=repository.head_sha,
            branch=None,
        )
        status = await self._git.inspect_status(repository.source_worktree)
        if status.head_sha != repository.head_sha:
            raise WorkspaceCheckpointError(
                "parent repository HEAD changed during result adoption inspection",
                kind=CheckpointFailureKind.CONCURRENT_MODIFICATION,
            )
        projection = await self._state.inspect(handle)
        if projection.head_sha != repository.head_sha:
            raise WorkspaceCheckpointError(
                "parent workspace projection HEAD changed during inspection",
                kind=CheckpointFailureKind.CONCURRENT_MODIFICATION,
            )
        return ParentWorkspaceSnapshot(repository=repository, projection=projection)


__all__ = ["LocalParentWorkspaceProjectionReader"]
