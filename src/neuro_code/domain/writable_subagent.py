"""Durable authority and lifecycle values for writable subagent workspaces.

Writable subagents do not receive a raw path as authority.  A grant is derived
from a Neuro-owned managed worktree and a READY baseline checkpoint; the lease
keeps that derivation discoverable across process crashes.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from neuro_code.domain.checkpoints import CheckpointId
from neuro_code.domain.worktree import (
    WorktreeHandle,
    WorktreeId,
    WorktreeRepositoryIdentity,
    WorktreeWorkspaceBinding,
)

MAX_WRITABLE_SUBAGENT_ID_BYTES = 128
MAX_WRITABLE_SUBAGENT_ERROR_BYTES = 1_000
MAX_WRITABLE_SUBAGENT_OWNER_TOKEN_BYTES = 256
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40,64}$")


def _safe_text(value: str, *, field_name: str, limit: int) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\x00" in value
        or len(value.encode("utf-8", "surrogateescape")) > limit
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{field_name} must be a bounded safe identifier")
    return value


def _digest(value: str, *, field_name: str) -> str:
    normalized = _safe_text(value, field_name=field_name, limit=64).casefold()
    if _SHA256_PATTERN.fullmatch(normalized) is None:
        raise ValueError(f"{field_name} must be a SHA-256 digest")
    return normalized


def _commit(value: str, *, field_name: str) -> str:
    normalized = _safe_text(value, field_name=field_name, limit=128).casefold()
    if _COMMIT_PATTERN.fullmatch(normalized) is None:
        raise ValueError(f"{field_name} must be a hexadecimal Git commit SHA")
    return normalized


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


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


class WritableSubagentWorkspaceState(StrEnum):
    """Durable phases for one explicitly allocated child workspace."""

    ALLOCATING = "allocating"
    WORKTREE_READY = "worktree_ready"
    BASELINE_READY = "baseline_ready"
    ACTIVE = "active"
    PRESERVED = "preserved"
    ORPHANED = "orphaned"
    FAILED = "failed"

    @property
    def active(self) -> bool:
        return self in {
            WritableSubagentWorkspaceState.ALLOCATING,
            WritableSubagentWorkspaceState.WORKTREE_READY,
            WritableSubagentWorkspaceState.BASELINE_READY,
            WritableSubagentWorkspaceState.ACTIVE,
        }

    @property
    def terminal(self) -> bool:
        return self in {
            WritableSubagentWorkspaceState.PRESERVED,
            WritableSubagentWorkspaceState.ORPHANED,
            WritableSubagentWorkspaceState.FAILED,
        }


@dataclass(frozen=True, slots=True)
class ManagedChildWorkspaceGrant:
    """Typed authority derived from one READY managed worktree and checkpoint."""

    grant_id: str
    parent_capability_fingerprint: str
    parent_workspace_root: Path
    parent_repository: WorktreeRepositoryIdentity
    base_commit_sha: str
    worktree: WorktreeHandle
    managed_worktree_id: WorktreeId
    canonical_child_root: Path
    created_at: datetime
    baseline_checkpoint_id: CheckpointId

    def __post_init__(self) -> None:
        _safe_text(
            self.grant_id, field_name="writable grant id", limit=MAX_WRITABLE_SUBAGENT_ID_BYTES
        )
        object.__setattr__(
            self,
            "parent_capability_fingerprint",
            _digest(
                self.parent_capability_fingerprint,
                field_name="parent capability fingerprint",
            ),
        )
        if not isinstance(self.parent_repository, WorktreeRepositoryIdentity):
            raise TypeError("writable grant parent repository must be canonical")
        object.__setattr__(
            self,
            "parent_workspace_root",
            _canonical_path(self.parent_workspace_root, field_name="parent workspace root"),
        )
        object.__setattr__(
            self,
            "base_commit_sha",
            _commit(self.base_commit_sha, field_name="writable grant base commit"),
        )
        if not _path_is_within(self.parent_workspace_root, self.parent_repository.source_worktree):
            raise ValueError("parent workspace root must belong to the parent repository")
        if not isinstance(self.worktree, WorktreeHandle):
            raise TypeError("writable grant worktree must be canonical")
        if not isinstance(self.managed_worktree_id, WorktreeId):
            raise TypeError("writable grant worktree id must be canonical")
        if self.worktree.worktree_id != self.managed_worktree_id:
            raise ValueError("writable grant worktree id does not match its handle")
        if self.worktree.base_commit_sha != self.base_commit_sha:
            raise ValueError("writable grant base commit does not match its handle")
        object.__setattr__(
            self,
            "canonical_child_root",
            _canonical_path(self.canonical_child_root, field_name="canonical child root"),
        )
        if self.canonical_child_root != self.worktree.path:
            raise ValueError("writable grant child root does not match its worktree handle")
        if not isinstance(self.baseline_checkpoint_id, CheckpointId):
            raise TypeError("writable grant baseline checkpoint must be canonical")
        if not isinstance(self.created_at, datetime) or self.created_at.tzinfo is None:
            raise ValueError("writable grant creation time must be timezone-aware")
        object.__setattr__(self, "created_at", self.created_at.astimezone(UTC))

    @property
    def fingerprint(self) -> str:
        """Return the non-secret identity of this exact derived authority."""

        payload = {
            "grant_id": self.grant_id,
            "parent_capability_fingerprint": self.parent_capability_fingerprint,
            "parent_workspace_root": str(self.parent_workspace_root),
            "parent_repository": {
                "common_dir": str(self.parent_repository.common_dir),
                "source_worktree": str(self.parent_repository.source_worktree),
                "git_dir": str(self.parent_repository.git_dir),
                "head_sha": self.parent_repository.head_sha,
            },
            "base_commit_sha": self.base_commit_sha,
            "worktree_id": self.managed_worktree_id.value,
            "worktree_path": str(self.canonical_child_root),
            "worktree_repository_id": self.worktree.repository.repository_id,
            "baseline_checkpoint_id": self.baseline_checkpoint_id.value,
            "created_at": self.created_at.isoformat(),
        }
        encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @property
    def workspace_binding(self) -> WorktreeWorkspaceBinding:
        """Derive the worker workspace authority from the managed handle."""

        return WorktreeWorkspaceBinding(primary_root=self.worktree.path)


@dataclass(frozen=True, slots=True)
class WritableSubagentWorkspaceLease:
    """Durable cross-store linkage and lifecycle projection."""

    lease_id: str
    parent_session_id: str
    parent_task_id: str
    worktree_id: WorktreeId
    parent_capability_fingerprint: str
    parent_workspace_root: Path
    parent_repository: WorktreeRepositoryIdentity
    base_commit_sha: str
    canonical_child_root: Path
    state: WritableSubagentWorkspaceState
    created_at: datetime
    updated_at: datetime
    worktree: WorktreeHandle | None = None
    baseline_checkpoint_id: CheckpointId | None = None
    child_session_id: str | None = None
    capability_fingerprint: str | None = None
    grant_fingerprint: str | None = None
    owner_pid: int | None = None
    owner_token: str = "unassigned"
    final_workspace_fingerprint: str | None = None
    workspace_changed: bool | None = None
    changed_file_count: int | None = None
    error_kind: str | None = None
    version: int = 0

    def __post_init__(self) -> None:
        for value, field_name in (
            (self.lease_id, "writable lease id"),
            (self.parent_session_id, "writable lease parent session id"),
            (self.parent_task_id, "writable lease parent task id"),
        ):
            _safe_text(value, field_name=field_name, limit=MAX_WRITABLE_SUBAGENT_ID_BYTES)
        if not isinstance(self.worktree_id, WorktreeId):
            raise TypeError("writable lease worktree id must be canonical")
        object.__setattr__(
            self,
            "parent_capability_fingerprint",
            _digest(
                self.parent_capability_fingerprint,
                field_name="lease parent capability fingerprint",
            ),
        )
        if not isinstance(self.parent_repository, WorktreeRepositoryIdentity):
            raise TypeError("writable lease parent repository must be canonical")
        object.__setattr__(
            self,
            "parent_workspace_root",
            _canonical_path(self.parent_workspace_root, field_name="lease parent workspace root"),
        )
        if not _path_is_within(self.parent_workspace_root, self.parent_repository.source_worktree):
            raise ValueError("lease parent workspace root must belong to the parent repository")
        object.__setattr__(
            self,
            "base_commit_sha",
            _commit(self.base_commit_sha, field_name="lease base commit"),
        )
        object.__setattr__(
            self,
            "canonical_child_root",
            _canonical_path(self.canonical_child_root, field_name="lease child root"),
        )
        if not isinstance(self.state, WritableSubagentWorkspaceState):
            raise TypeError("writable lease state must be canonical")
        for timestamp, field_name in (
            (self.created_at, "created_at"),
            (self.updated_at, "updated_at"),
        ):
            if not isinstance(timestamp, datetime) or timestamp.tzinfo is None:
                raise ValueError(f"writable lease {field_name} must be timezone-aware")
        object.__setattr__(self, "created_at", self.created_at.astimezone(UTC))
        object.__setattr__(self, "updated_at", self.updated_at.astimezone(UTC))
        if self.updated_at < self.created_at:
            raise ValueError("writable lease update time must not precede creation time")
        if self.worktree is not None:
            if not isinstance(self.worktree, WorktreeHandle):
                raise TypeError("writable lease worktree must be canonical")
            if self.worktree.worktree_id != self.worktree_id:
                raise ValueError("writable lease worktree id does not match its handle")
            if self.worktree.path != self.canonical_child_root:
                raise ValueError("writable lease child root does not match its handle")
            if self.worktree.base_commit_sha != self.base_commit_sha:
                raise ValueError("writable lease base commit does not match its handle")
        if self.baseline_checkpoint_id is not None and not isinstance(
            self.baseline_checkpoint_id, CheckpointId
        ):
            raise TypeError("writable lease checkpoint id must be canonical")
        for text_value, field_name in (
            (self.child_session_id, "writable lease child session id"),
            (self.owner_token, "writable lease owner token"),
            (self.error_kind, "writable lease error kind"),
        ):
            if text_value is not None:
                _safe_text(
                    text_value,
                    field_name=field_name,
                    limit=(
                        MAX_WRITABLE_SUBAGENT_OWNER_TOKEN_BYTES
                        if field_name.endswith("owner token")
                        else MAX_WRITABLE_SUBAGENT_ERROR_BYTES
                    ),
                )
        for digest_value, field_name in (
            (self.capability_fingerprint, "writable lease capability fingerprint"),
            (self.grant_fingerprint, "writable lease grant fingerprint"),
            (self.final_workspace_fingerprint, "final workspace fingerprint"),
        ):
            if digest_value is not None:
                _digest(digest_value, field_name=field_name)
        if self.owner_pid is not None and (
            isinstance(self.owner_pid, bool)
            or not isinstance(self.owner_pid, int)
            or self.owner_pid <= 0
        ):
            raise ValueError("writable lease owner pid is invalid")
        if self.workspace_changed is not None and not isinstance(self.workspace_changed, bool):
            raise TypeError("writable lease changed flag must be boolean or None")
        if self.changed_file_count is not None and (
            isinstance(self.changed_file_count, bool)
            or not isinstance(self.changed_file_count, int)
            or self.changed_file_count < 0
        ):
            raise ValueError("writable lease changed file count must be non-negative")
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 0:
            raise ValueError("writable lease version must be non-negative")

    @property
    def grant_ready(self) -> bool:
        return (
            self.worktree is not None
            and self.baseline_checkpoint_id is not None
            and self.capability_fingerprint is not None
            and self.grant_fingerprint is not None
        )

    @property
    def grant(self) -> ManagedChildWorkspaceGrant:
        if self.worktree is None or self.baseline_checkpoint_id is None:
            raise ValueError("writable lease does not yet contain a derived grant")
        return ManagedChildWorkspaceGrant(
            grant_id=self.lease_id,
            parent_capability_fingerprint=self.parent_capability_fingerprint,
            parent_workspace_root=self.parent_workspace_root,
            parent_repository=self.parent_repository,
            base_commit_sha=self.base_commit_sha,
            worktree=self.worktree,
            managed_worktree_id=self.worktree_id,
            canonical_child_root=self.canonical_child_root,
            created_at=self.created_at,
            baseline_checkpoint_id=self.baseline_checkpoint_id,
        )

    @property
    def effective_fingerprint(self) -> str | None:
        if self.capability_fingerprint is None or self.grant_fingerprint is None:
            return None
        encoded = f"{self.capability_fingerprint}:{self.grant_fingerprint}".encode()
        return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "MAX_WRITABLE_SUBAGENT_ERROR_BYTES",
    "MAX_WRITABLE_SUBAGENT_ID_BYTES",
    "MAX_WRITABLE_SUBAGENT_OWNER_TOKEN_BYTES",
    "ManagedChildWorkspaceGrant",
    "WritableSubagentWorkspaceLease",
    "WritableSubagentWorkspaceState",
]
