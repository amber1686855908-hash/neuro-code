"""Domain values for application-owned Git worktree lifecycles.

Git's porcelain output and filesystem side effects stay outside this module.
The values here are immutable so a future worker can hold a bounded handle
without receiving a raw path-only capability.

定义由应用拥有的 Git worktree 生命周期领域值.

Git 的 porcelain 输出和文件系统副作用不属于本模块.这里的值不可变,未来 worker
可以持有有界 handle,而不是只接收一个裸路径能力.
"""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

MAX_WORKTREE_ID_BYTES = 128
MAX_WORKTREE_REVISION_BYTES = 512
MAX_WORKTREE_BRANCH_BYTES = 512
MAX_WORKTREE_REPOSITORY_ID_BYTES = 64

_WORKTREE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")
_HEX_SHA_PATTERN = re.compile(r"^[0-9a-f]{40,64}$")


def _canonical_path(value: Path, *, field_name: str) -> Path:
    if not isinstance(value, Path):
        raise TypeError(f"{field_name} must be a pathlib.Path")
    try:
        resolved = value.expanduser().resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise ValueError(f"{field_name} cannot be canonicalized") from error
    if not resolved.is_absolute():
        raise ValueError(f"{field_name} must be absolute")
    return resolved


def _bounded_text(value: str, *, field_name: str, limit: int) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"{field_name} must be non-empty text without NUL")
    if len(value.encode("utf-8")) > limit:
        raise ValueError(f"{field_name} is too long")
    return value


def _validate_sha(value: str, *, field_name: str) -> str:
    normalized = _bounded_text(value, field_name=field_name, limit=MAX_WORKTREE_REVISION_BYTES)
    if _HEX_SHA_PATTERN.fullmatch(normalized.casefold()) is None:
        raise ValueError(f"{field_name} must be a hexadecimal Git commit SHA")
    return normalized.casefold()


class WorktreeId:
    """Opaque, bounded identifier for one Neuro Code-managed worktree."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        normalized = _bounded_text(value, field_name="worktree id", limit=MAX_WORKTREE_ID_BYTES)
        if _WORKTREE_ID_PATTERN.fullmatch(normalized) is None:
            raise ValueError("worktree id must use lowercase letters, digits, '_' or '-'")
        self._value = normalized

    @classmethod
    def new(cls) -> WorktreeId:
        return cls(f"wt-{uuid.uuid4().hex}")

    @property
    def value(self) -> str:
        return self._value

    def __str__(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return f"WorktreeId({self._value!r})"

    def __hash__(self) -> int:
        return hash(self._value)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, WorktreeId) and self._value == other._value


class WorktreeKind(StrEnum):
    """The two creation modes supported by the first capability slice."""

    DETACHED = "detached"
    MANAGED_BRANCH = "managed_branch"


class WorktreeState(StrEnum):
    """Durable lifecycle states used by intent and reconciliation."""

    CREATING = "creating"
    READY = "ready"
    REMOVING = "removing"
    REMOVED = "removed"
    ORPHANED = "orphaned"
    FAILED = "failed"


class WorktreeOwnership(StrEnum):
    """Ownership is explicit; directory names are never authority."""

    MANAGED = "managed"
    EXTERNAL = "external"


@dataclass(frozen=True, slots=True)
class WorktreeRepositoryIdentity:
    """Canonical identity shared by a main checkout and linked worktrees."""

    common_dir: Path
    source_worktree: Path
    git_dir: Path
    head_sha: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "common_dir", _canonical_path(self.common_dir, field_name="Git common dir")
        )
        object.__setattr__(
            self,
            "source_worktree",
            _canonical_path(self.source_worktree, field_name="source worktree"),
        )
        object.__setattr__(self, "git_dir", _canonical_path(self.git_dir, field_name="Git dir"))
        object.__setattr__(
            self, "head_sha", _validate_sha(self.head_sha, field_name="repository HEAD")
        )

    @property
    def repository_id(self) -> str:
        """Return a stable, path-safe identity for managed-root partitioning."""

        normalized = os.path.normcase(os.fspath(self.common_dir))
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class WorktreeStatus:
    """Bounded observed status of one actual Git worktree."""

    path: Path
    head_sha: str
    branch: str | None = None
    detached: bool = False
    dirty: bool = False
    changed_file_count: int = 0
    locked: bool = False
    prunable: bool = False
    exists: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _canonical_path(self.path, field_name="worktree path"))
        object.__setattr__(
            self, "head_sha", _validate_sha(self.head_sha, field_name="worktree HEAD")
        )
        if self.branch is not None:
            _bounded_text(
                self.branch, field_name="worktree branch", limit=MAX_WORKTREE_BRANCH_BYTES
            )
        if not isinstance(self.detached, bool):
            raise TypeError("worktree detached flag must be boolean")
        if not isinstance(self.dirty, bool):
            raise TypeError("worktree dirty flag must be boolean")
        if (
            isinstance(self.changed_file_count, bool)
            or not isinstance(self.changed_file_count, int)
            or self.changed_file_count < 0
        ):
            raise ValueError("worktree changed file count must be non-negative")
        if not isinstance(self.locked, bool) or not isinstance(self.prunable, bool):
            raise TypeError("worktree lock/prunable flags must be boolean")
        if not isinstance(self.exists, bool):
            raise TypeError("worktree exists flag must be boolean")
        if self.detached and self.branch is not None:
            raise ValueError("detached worktree cannot expose a branch")
        if not self.detached and self.branch is None:
            raise ValueError("attached worktree must expose a branch")


@dataclass(frozen=True, slots=True)
class WorktreeCreateRequest:
    """Explicit request for creating a clean worktree at one exact revision."""

    repository_path: Path
    base_revision: str
    kind: WorktreeKind = WorktreeKind.DETACHED
    worktree_id: WorktreeId | None = None
    branch: str | None = None
    created_by_session_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "repository_path",
            _canonical_path(self.repository_path, field_name="repository path"),
        )
        object.__setattr__(
            self,
            "base_revision",
            _bounded_text(
                self.base_revision,
                field_name="base revision",
                limit=MAX_WORKTREE_REVISION_BYTES,
            ),
        )
        if not isinstance(self.kind, WorktreeKind):
            raise TypeError("worktree kind must be canonical")
        if self.worktree_id is not None and not isinstance(self.worktree_id, WorktreeId):
            raise TypeError("worktree id must be canonical")
        if self.branch is not None:
            _bounded_text(
                self.branch, field_name="worktree branch", limit=MAX_WORKTREE_BRANCH_BYTES
            )
        if self.created_by_session_id is not None:
            _bounded_text(
                self.created_by_session_id,
                field_name="creating session id",
                limit=256,
            )
        if self.kind is WorktreeKind.DETACHED and self.branch is not None:
            raise ValueError("detached worktree must not request a branch")


@dataclass(frozen=True, slots=True)
class WorktreeRemoveRequest:
    """Explicit non-force removal request.

    Branches are intentionally retained.  Branch deletion is a separate future
    capability so removing a worktree cannot erase a worker's unintegrated ref.
    """

    worktree_id: WorktreeId
    delete_branch: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.worktree_id, WorktreeId):
            raise TypeError("worktree id must be canonical")
        if not isinstance(self.delete_branch, bool):
            raise TypeError("delete branch flag must be boolean")
        if self.delete_branch:
            raise ValueError("managed branch deletion is not part of this capability")


@dataclass(frozen=True, slots=True)
class WorktreeSnapshot:
    """Durable ownership record and its latest lifecycle projection."""

    worktree_id: WorktreeId
    repository: WorktreeRepositoryIdentity
    canonical_path: Path
    base_revision: str
    base_commit_sha: str
    branch: str | None
    kind: WorktreeKind
    ownership: WorktreeOwnership
    state: WorktreeState
    created_at: datetime
    managed: bool = True
    created_by_session_id: str | None = None
    status: WorktreeStatus | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.worktree_id, WorktreeId):
            raise TypeError("worktree snapshot id must be canonical")
        if not isinstance(self.repository, WorktreeRepositoryIdentity):
            raise TypeError("worktree snapshot repository must be canonical")
        object.__setattr__(
            self,
            "canonical_path",
            _canonical_path(self.canonical_path, field_name="worktree path"),
        )
        object.__setattr__(
            self,
            "base_revision",
            _bounded_text(
                self.base_revision,
                field_name="base revision",
                limit=MAX_WORKTREE_REVISION_BYTES,
            ),
        )
        object.__setattr__(
            self,
            "base_commit_sha",
            _validate_sha(self.base_commit_sha, field_name="base commit SHA"),
        )
        if self.branch is not None:
            _bounded_text(
                self.branch, field_name="worktree branch", limit=MAX_WORKTREE_BRANCH_BYTES
            )
        if not isinstance(self.kind, WorktreeKind):
            raise TypeError("worktree snapshot kind must be canonical")
        if not isinstance(self.ownership, WorktreeOwnership):
            raise TypeError("worktree snapshot ownership must be canonical")
        if not isinstance(self.state, WorktreeState):
            raise TypeError("worktree snapshot state must be canonical")
        if self.created_at.tzinfo is None:
            raise ValueError("worktree creation time must be timezone-aware")
        object.__setattr__(self, "created_at", self.created_at.astimezone(UTC))
        if not isinstance(self.managed, bool):
            raise TypeError("worktree managed flag must be boolean")
        if self.ownership is WorktreeOwnership.MANAGED and not self.managed:
            raise ValueError("managed ownership requires managed=true")
        if self.created_by_session_id is not None:
            _bounded_text(
                self.created_by_session_id,
                field_name="creating session id",
                limit=256,
            )
        if self.status is not None and not isinstance(self.status, WorktreeStatus):
            raise TypeError("worktree snapshot status must be canonical")
        if self.kind is WorktreeKind.DETACHED and self.branch is not None:
            raise ValueError("detached snapshot must not expose a branch")
        if self.kind is WorktreeKind.MANAGED_BRANCH and self.branch is None:
            raise ValueError("managed branch snapshot must expose a branch")

    @property
    def handle(self) -> WorktreeHandle:
        return WorktreeHandle(
            worktree_id=self.worktree_id,
            repository=self.repository,
            path=self.canonical_path,
            base_commit_sha=self.base_commit_sha,
            branch=self.branch,
        )


@dataclass(frozen=True, slots=True)
class WorktreeHandle:
    """Immutable capability passed to future workspace-bound workers."""

    worktree_id: WorktreeId
    repository: WorktreeRepositoryIdentity
    path: Path
    base_commit_sha: str
    branch: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.worktree_id, WorktreeId):
            raise TypeError("worktree handle id must be canonical")
        if not isinstance(self.repository, WorktreeRepositoryIdentity):
            raise TypeError("worktree handle repository must be canonical")
        object.__setattr__(
            self, "path", _canonical_path(self.path, field_name="worktree handle path")
        )
        object.__setattr__(
            self,
            "base_commit_sha",
            _validate_sha(self.base_commit_sha, field_name="worktree handle base commit"),
        )
        if self.branch is not None:
            _bounded_text(
                self.branch, field_name="worktree handle branch", limit=MAX_WORKTREE_BRANCH_BYTES
            )


@dataclass(frozen=True, slots=True)
class WorktreeWorkspaceBinding:
    """Independent workspace roots derived from a managed worktree handle."""

    primary_root: Path
    additional_roots: tuple[Path, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "primary_root", _canonical_path(self.primary_root, field_name="primary root")
        )
        roots = tuple(
            _canonical_path(root, field_name="additional root") for root in self.additional_roots
        )
        if any(
            root == self.primary_root or root.is_relative_to(self.primary_root) for root in roots
        ):
            raise ValueError("additional roots must not overlap the primary worktree root")
        if any(
            first == second or first.is_relative_to(second) or second.is_relative_to(first)
            for index, first in enumerate(roots)
            for second in roots[index + 1 :]
        ):
            raise ValueError("additional roots must not overlap")
        object.__setattr__(self, "additional_roots", roots)


__all__ = [
    "MAX_WORKTREE_BRANCH_BYTES",
    "MAX_WORKTREE_ID_BYTES",
    "MAX_WORKTREE_REPOSITORY_ID_BYTES",
    "MAX_WORKTREE_REVISION_BYTES",
    "WorktreeCreateRequest",
    "WorktreeHandle",
    "WorktreeId",
    "WorktreeKind",
    "WorktreeOwnership",
    "WorktreeRemoveRequest",
    "WorktreeRepositoryIdentity",
    "WorktreeSnapshot",
    "WorktreeState",
    "WorktreeStatus",
    "WorktreeWorkspaceBinding",
]
