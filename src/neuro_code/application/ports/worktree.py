"""Canonical application ports for the managed Git worktree capability.

The Git adapter owns argv construction, subprocess execution, and porcelain
parsing.  Application code consumes only these typed values and operations.

定义受管 Git worktree 能力的规范应用端口.

Git 适配器拥有 argv 构造、子进程执行和 porcelain 解析;应用代码只消费这些类型化值和操作.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from neuro_code.domain.worktree import (
    WorktreeCreateRequest,
    WorktreeRemoveRequest,
    WorktreeRepositoryIdentity,
    WorktreeSnapshot,
    WorktreeState,
    WorktreeStatus,
)

MAX_GIT_ERROR_BYTES = 1_000
MAX_GIT_OUTPUT_BYTES = 1_000_000
MAX_GIT_COMMAND_TIMEOUT_SECONDS = 120.0
MINIMUM_GIT_VERSION = (2, 40, 0)


class WorktreeFailureKind(StrEnum):
    """Bounded facts for failures at the local Git boundary."""

    NOT_AVAILABLE = "not_available"
    NOT_REPOSITORY = "not_repository"
    REPOSITORY_MISSING = "repository_missing"
    INVALID_REVISION = "invalid_revision"
    INVALID_REF = "invalid_ref"
    COMMAND_FAILED = "command_failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    OUTPUT_LIMIT = "output_limit"
    PROTOCOL = "protocol"
    PATH_CONFLICT = "path_conflict"
    BRANCH_CONFLICT = "branch_conflict"
    IDENTITY_MISMATCH = "identity_mismatch"
    DIRTY = "dirty"
    LOCKED = "locked"
    UNMANAGED = "unmanaged"
    FAILED_STATE = "failed_state"
    EXTERNAL_FILTER_UNSUPPORTED = "external_filter_unsupported"
    UNSAFE_GIT_CONFIGURATION = "unsafe_git_configuration"
    CONCURRENT_MODIFICATION = "concurrent_modification"


class WorktreeError(Exception):
    """Expected, typed, bounded failure from a worktree operation."""

    def __init__(self, message: str, *, kind: WorktreeFailureKind) -> None:
        self.kind = kind
        super().__init__(message[:MAX_GIT_ERROR_BYTES])


@dataclass(frozen=True, slots=True)
class GitWorktreeRecord:
    """Typed projection of one ``git worktree list --porcelain`` record."""

    path: Path
    head_sha: str
    branch: str | None = None
    detached: bool = False
    locked: bool = False
    prunable: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.path, Path) or not self.path.is_absolute():
            raise ValueError("Git worktree record path must be absolute")
        try:
            canonical_path = self.path.expanduser().resolve(strict=False)
        except (OSError, RuntimeError) as error:
            raise ValueError("Git worktree record path must be canonicalizable") from error
        object.__setattr__(self, "path", canonical_path)
        if not isinstance(self.head_sha, str) or not self.head_sha:
            raise ValueError("Git worktree record HEAD must be non-empty")
        if self.branch is not None and (not self.branch or "\x00" in self.branch):
            raise ValueError("Git worktree record branch is invalid")
        if self.detached and self.branch is not None:
            raise ValueError("detached Git worktree record cannot expose a branch")
        if not self.detached and self.branch is None:
            raise ValueError("attached Git worktree record must expose a branch")
        if not isinstance(self.locked, bool) or not isinstance(self.prunable, bool):
            raise TypeError("Git worktree record flags must be boolean")


class GitWorktreePort(Protocol):
    """Local-only typed Git boundary used by the application service."""

    async def repository_identity(self, path: Path, /) -> WorktreeRepositoryIdentity: ...

    async def resolve_commit(self, path: Path, revision: str, /) -> str: ...

    async def validate_branch(self, path: Path, branch: str, /) -> str: ...

    async def branch_exists(self, path: Path, branch: str, /) -> bool: ...

    async def preflight_checkout(self, path: Path, commit_sha: str, /) -> None: ...

    async def list_worktrees(self, path: Path, /) -> tuple[GitWorktreeRecord, ...]: ...

    async def add_worktree(
        self,
        repository_path: Path,
        target_path: Path,
        commit_sha: str,
        *,
        branch: str | None,
    ) -> None: ...

    async def inspect_status(self, path: Path, /) -> WorktreeStatus: ...

    async def remove_worktree(self, repository_path: Path, target_path: Path, /) -> None: ...

    async def git_version(self) -> tuple[int, int, int]: ...


class ManagedWorktreeStore(Protocol):
    """Durable ownership record port, separate from session recovery."""

    async def initialize(self) -> None: ...

    async def get(self, worktree_id: str, /) -> WorktreeSnapshot | None: ...

    async def list(
        self,
        *,
        include_removed: bool = False,
        repository_id: str | None = None,
    ) -> tuple[WorktreeSnapshot, ...]: ...

    async def insert_intent(self, snapshot: WorktreeSnapshot, /) -> WorktreeSnapshot: ...

    async def compare_and_transition(
        self,
        snapshot: WorktreeSnapshot,
        *,
        expected_version: int,
        expected_state: WorktreeState | None = None,
    ) -> WorktreeSnapshot: ...


class WorktreeApplication(Protocol):
    """Inbound application capability for managed worktree lifecycle."""

    async def create(self, request: WorktreeCreateRequest) -> WorktreeSnapshot: ...

    async def list_managed(self, *, reconcile: bool = True) -> tuple[WorktreeSnapshot, ...]: ...

    async def inspect(self, worktree_id: str, /) -> WorktreeSnapshot: ...

    async def remove(self, request: WorktreeRemoveRequest) -> WorktreeSnapshot: ...


__all__ = [
    "MAX_GIT_COMMAND_TIMEOUT_SECONDS",
    "MAX_GIT_ERROR_BYTES",
    "MAX_GIT_OUTPUT_BYTES",
    "MINIMUM_GIT_VERSION",
    "GitWorktreePort",
    "GitWorktreeRecord",
    "ManagedWorktreeStore",
    "WorktreeApplication",
    "WorktreeError",
    "WorktreeFailureKind",
]
