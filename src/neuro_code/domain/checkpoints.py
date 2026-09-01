"""Immutable values for managed workspace checkpoints and rollback attempts.

Workspace checkpoints are deliberately separate from execution-segment
checkpoints and turn-recovery records.  They describe only the source
projection of one Neuro Code-owned managed worktree.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath

from neuro_code.domain.worktree import WorktreeHandle, WorktreeId, WorktreeRepositoryIdentity

MAX_CHECKPOINT_ID_BYTES = 128
MAX_ROLLBACK_ATTEMPT_ID_BYTES = 128
MAX_CHECKPOINT_ERROR_BYTES = 1_000
MAX_CHECKPOINT_LINK_TARGET_BYTES = 32 * 1024
MAX_CHECKPOINT_INDEX_BYTES = 64 * 1024 * 1024

_CHECKPOINT_ID_PATTERN = re.compile(r"^cp-[a-z0-9][a-z0-9_-]{0,124}$")
_ROLLBACK_ATTEMPT_ID_PATTERN = re.compile(r"^rb-[a-z0-9][a-z0-9_-]{0,124}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _bounded_text(value: str, *, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"{name} must be non-empty text without NUL")
    if len(value.encode("utf-8", "surrogateescape")) > limit:
        raise ValueError(f"{name} is too long")
    return value


def _sha256(value: str, *, name: str) -> str:
    normalized = _bounded_text(value, name=name, limit=64).casefold()
    if _SHA256_PATTERN.fullmatch(normalized) is None:
        raise ValueError(f"{name} must be a SHA-256 digest")
    return normalized


def _canonical_path(value: Path, *, name: str) -> Path:
    if not isinstance(value, Path):
        raise TypeError(f"{name} must be a pathlib.Path")
    try:
        resolved = value.expanduser().resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise ValueError(f"{name} cannot be canonicalized") from error
    if not resolved.is_absolute():
        raise ValueError(f"{name} must be absolute")
    return resolved


def _validate_relative_path(value: str) -> str:
    path = _bounded_text(value, name="checkpoint relative path", limit=32 * 1024)
    if "\\" in path or path.startswith("/"):
        raise ValueError("checkpoint paths must be relative POSIX paths")
    parts = PurePosixPath(path).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("checkpoint paths must not contain traversal components")
    if ":" in parts[0]:
        raise ValueError("checkpoint paths must not contain a drive prefix")
    return "/".join(parts)


class CheckpointId:
    """Opaque durable identifier for one immutable workspace checkpoint."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        normalized = _bounded_text(value, name="checkpoint id", limit=MAX_CHECKPOINT_ID_BYTES)
        if _CHECKPOINT_ID_PATTERN.fullmatch(normalized) is None:
            raise ValueError("checkpoint id must use the cp- prefix and safe lowercase characters")
        self._value = normalized

    @classmethod
    def new(cls) -> CheckpointId:
        return cls(f"cp-{uuid.uuid4().hex}")

    @property
    def value(self) -> str:
        return self._value

    def __str__(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return f"CheckpointId({self._value!r})"

    def __hash__(self) -> int:
        return hash(self._value)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, CheckpointId) and self._value == other._value


class RollbackAttemptId:
    """Opaque durable identifier for one rollback operation."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        normalized = _bounded_text(
            value,
            name="rollback attempt id",
            limit=MAX_ROLLBACK_ATTEMPT_ID_BYTES,
        )
        if _ROLLBACK_ATTEMPT_ID_PATTERN.fullmatch(normalized) is None:
            raise ValueError(
                "rollback attempt id must use the rb- prefix and safe lowercase characters"
            )
        self._value = normalized

    @classmethod
    def new(cls) -> RollbackAttemptId:
        return cls(f"rb-{uuid.uuid4().hex}")

    @property
    def value(self) -> str:
        return self._value

    def __str__(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return f"RollbackAttemptId({self._value!r})"

    def __hash__(self) -> int:
        return hash(self._value)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, RollbackAttemptId) and self._value == other._value


@dataclass(frozen=True, slots=True)
class CheckpointFingerprint:
    """Cryptographic identity of the captured in-scope workspace projection."""

    value: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _sha256(self.value, name="checkpoint fingerprint"))


class CheckpointState(StrEnum):
    CAPTURING = "capturing"
    READY = "ready"
    FAILED = "failed"


class RollbackState(StrEnum):
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"
    INDETERMINATE = "indeterminate"


class WorkspaceFileScope(StrEnum):
    TRACKED = "tracked"
    UNTRACKED = "untracked"


class WorkspaceFileKind(StrEnum):
    REGULAR = "regular"
    SYMLINK = "symlink"


@dataclass(frozen=True, slots=True)
class WorkspaceFileEntry:
    """One source-controlled projection entry, never an arbitrary filesystem path."""

    path: str
    scope: WorkspaceFileScope
    present: bool
    kind: WorkspaceFileKind
    mode: int
    content: bytes | None = None
    link_target: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _validate_relative_path(self.path))
        if not isinstance(self.scope, WorkspaceFileScope):
            raise TypeError("workspace file scope must be canonical")
        if not isinstance(self.present, bool):
            raise TypeError("workspace file presence must be boolean")
        if not isinstance(self.kind, WorkspaceFileKind):
            raise TypeError("workspace file kind must be canonical")
        if (
            isinstance(self.mode, bool)
            or not isinstance(self.mode, int)
            or self.mode < 0
            or self.mode > 0o177777
        ):
            raise ValueError("workspace file mode is invalid")
        if not self.present:
            if self.content is not None or self.link_target is not None:
                raise ValueError("absent workspace file cannot carry content")
            return
        if self.kind is WorkspaceFileKind.REGULAR:
            if not isinstance(self.content, bytes):
                raise TypeError("regular workspace file must carry bytes")
            if self.link_target is not None:
                raise ValueError("regular workspace file cannot carry a link target")
        else:
            if self.content is not None:
                raise ValueError("symlink workspace file cannot carry regular content")
            if not isinstance(self.link_target, str) or "\x00" in self.link_target:
                raise ValueError("symlink workspace file must carry a safe link target")
            if (
                len(self.link_target.encode("utf-8", "surrogateescape"))
                > MAX_CHECKPOINT_LINK_TARGET_BYTES
            ):
                raise ValueError("symlink workspace link target is too long")


@dataclass(frozen=True, slots=True)
class WorkspaceProjection:
    """Complete bounded projection used for fingerprinting and exact restore."""

    head_sha: str
    branch: str | None
    detached: bool
    index_bytes: bytes
    entries: tuple[WorkspaceFileEntry, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.head_sha, str) or not self.head_sha:
            raise ValueError("workspace projection HEAD must be non-empty")
        if not isinstance(self.branch, str) and self.branch is not None:
            raise TypeError("workspace projection branch must be text or None")
        if not isinstance(self.detached, bool):
            raise TypeError("workspace projection detached flag must be boolean")
        if self.detached and self.branch is not None:
            raise ValueError("detached projection cannot expose a branch")
        if not self.detached and self.branch is None:
            raise ValueError("attached projection must expose a branch")
        if not isinstance(self.index_bytes, bytes):
            raise TypeError("workspace projection index must be bytes")
        if len(self.index_bytes) > MAX_CHECKPOINT_INDEX_BYTES:
            raise ValueError("workspace projection index is too large")
        entries = tuple(self.entries)
        if not all(isinstance(entry, WorkspaceFileEntry) for entry in entries):
            raise TypeError("workspace projection entries must be canonical")
        if tuple(sorted(entries, key=lambda entry: (entry.path, entry.scope.value))) != entries:
            raise ValueError("workspace projection entries must be deterministic")
        keys = [(entry.scope.value, entry.path) for entry in entries]
        if len(keys) != len(set(keys)):
            raise ValueError("workspace projection contains duplicate entries")
        object.__setattr__(self, "entries", entries)


def workspace_projection_payload(
    handle: WorktreeHandle,
    projection: WorkspaceProjection,
    *,
    include_path: bool = True,
) -> dict[str, object]:
    """Return the deterministic, content-addressed projection description."""

    entries: list[dict[str, object]] = []
    for entry in projection.entries:
        if entry.present:
            if entry.kind is WorkspaceFileKind.REGULAR:
                assert entry.content is not None
                digest = hashlib.sha256(entry.content).hexdigest()
                size = len(entry.content)
                link_target = None
            else:
                assert entry.link_target is not None
                encoded_target = entry.link_target.encode("utf-8", "surrogateescape")
                digest = hashlib.sha256(encoded_target).hexdigest()
                size = len(encoded_target)
                link_target = entry.link_target
        else:
            digest = None
            size = 0
            link_target = None
        entries.append(
            {
                "path": entry.path,
                "scope": entry.scope.value,
                "present": entry.present,
                "kind": entry.kind.value,
                "mode": entry.mode,
                "size": size,
                "sha256": digest,
                "link_target": link_target,
            }
        )
    payload: dict[str, object] = {
        "format": 1,
        "worktree_id": handle.worktree_id.value,
        "repository_id": handle.repository.repository_id,
        "head_sha": projection.head_sha,
        "branch": projection.branch,
        "detached": projection.detached,
        "index_sha256": hashlib.sha256(projection.index_bytes).hexdigest(),
        "entries": entries,
    }
    if include_path:
        payload["canonical_path"] = str(handle.path)
    return payload


def workspace_projection_fingerprint(
    handle: WorktreeHandle,
    projection: WorkspaceProjection,
) -> CheckpointFingerprint:
    """Hash repository/worktree identity plus the complete in-scope projection."""

    encoded = json.dumps(
        workspace_projection_payload(handle, projection),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return CheckpointFingerprint(hashlib.sha256(encoded).hexdigest())


@dataclass(frozen=True, slots=True)
class CheckpointCreateRequest:
    """Capture request bound to a managed handle, never a raw user path."""

    worktree: WorktreeHandle
    checkpoint_id: CheckpointId | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.worktree, WorktreeHandle):
            raise TypeError("checkpoint request must carry a managed worktree handle")
        if self.checkpoint_id is not None and not isinstance(self.checkpoint_id, CheckpointId):
            raise TypeError("checkpoint id must be canonical")


@dataclass(frozen=True, slots=True)
class WorkspaceCheckpoint:
    """Immutable durable checkpoint metadata."""

    checkpoint_id: CheckpointId
    worktree_id: WorktreeId
    repository: WorktreeRepositoryIdentity
    canonical_path: Path
    head_sha: str
    branch: str | None
    detached: bool
    created_at: datetime
    source_fingerprint: CheckpointFingerprint
    artifact_path: Path
    artifact_sha256: str
    artifact_bytes: int
    artifact_file_count: int
    state: CheckpointState
    version: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.checkpoint_id, CheckpointId):
            raise TypeError("checkpoint id must be canonical")
        if not isinstance(self.worktree_id, WorktreeId):
            raise TypeError("checkpoint worktree id must be canonical")
        if not isinstance(self.repository, WorktreeRepositoryIdentity):
            raise TypeError("checkpoint repository identity must be canonical")
        object.__setattr__(
            self,
            "canonical_path",
            _canonical_path(self.canonical_path, name="checkpoint worktree path"),
        )
        object.__setattr__(
            self, "head_sha", _bounded_text(self.head_sha, name="checkpoint HEAD", limit=128)
        )
        if self.branch is not None:
            _bounded_text(self.branch, name="checkpoint branch", limit=512)
        if not isinstance(self.detached, bool):
            raise TypeError("checkpoint detached flag must be boolean")
        if self.detached and self.branch is not None:
            raise ValueError("detached checkpoint cannot expose a branch")
        if not self.detached and self.branch is None:
            raise ValueError("attached checkpoint must expose a branch")
        if self.created_at.tzinfo is None:
            raise ValueError("checkpoint timestamp must be timezone-aware")
        object.__setattr__(self, "created_at", self.created_at.astimezone(UTC))
        if not isinstance(self.source_fingerprint, CheckpointFingerprint):
            raise TypeError("checkpoint fingerprint must be canonical")
        object.__setattr__(
            self,
            "artifact_path",
            _canonical_path(self.artifact_path, name="checkpoint artifact path"),
        )
        object.__setattr__(
            self,
            "artifact_sha256",
            _sha256(self.artifact_sha256, name="checkpoint artifact digest"),
        )
        for name in ("artifact_bytes", "artifact_file_count", "version"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"checkpoint {name} must be non-negative")
        if not isinstance(self.state, CheckpointState):
            raise TypeError("checkpoint state must be canonical")


@dataclass(frozen=True, slots=True)
class RollbackAttempt:
    """Durable rollback operation state, separate from immutable checkpoint data."""

    attempt_id: RollbackAttemptId
    checkpoint_id: CheckpointId
    worktree_id: WorktreeId
    state: RollbackState
    started_at: datetime
    completed_at: datetime | None
    expected_fingerprint: CheckpointFingerprint
    observed_fingerprint: CheckpointFingerprint | None = None
    owner_pid: int | None = None
    owner_token: str = "unassigned"
    error_kind: str | None = None
    version: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.attempt_id, RollbackAttemptId):
            raise TypeError("rollback attempt id must be canonical")
        if not isinstance(self.checkpoint_id, CheckpointId):
            raise TypeError("rollback checkpoint id must be canonical")
        if not isinstance(self.worktree_id, WorktreeId):
            raise TypeError("rollback worktree id must be canonical")
        if not isinstance(self.state, RollbackState):
            raise TypeError("rollback state must be canonical")
        if self.started_at.tzinfo is None:
            raise ValueError("rollback start timestamp must be timezone-aware")
        object.__setattr__(self, "started_at", self.started_at.astimezone(UTC))
        if self.completed_at is not None:
            if self.completed_at.tzinfo is None:
                raise ValueError("rollback completion timestamp must be timezone-aware")
            object.__setattr__(self, "completed_at", self.completed_at.astimezone(UTC))
        if not isinstance(self.expected_fingerprint, CheckpointFingerprint):
            raise TypeError("rollback expected fingerprint must be canonical")
        if self.owner_pid is not None and (
            isinstance(self.owner_pid, bool)
            or not isinstance(self.owner_pid, int)
            or self.owner_pid <= 0
        ):
            raise ValueError("rollback owner pid is invalid")
        _bounded_text(self.owner_token, name="rollback owner token", limit=256)
        if self.error_kind is not None:
            _bounded_text(
                self.error_kind, name="rollback error kind", limit=MAX_CHECKPOINT_ERROR_BYTES
            )
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 0:
            raise ValueError("rollback attempt version must be non-negative")


__all__ = [
    "MAX_CHECKPOINT_ERROR_BYTES",
    "MAX_CHECKPOINT_INDEX_BYTES",
    "MAX_CHECKPOINT_LINK_TARGET_BYTES",
    "CheckpointCreateRequest",
    "CheckpointFingerprint",
    "CheckpointId",
    "CheckpointState",
    "RollbackAttempt",
    "RollbackAttemptId",
    "RollbackState",
    "WorkspaceCheckpoint",
    "WorkspaceFileEntry",
    "WorkspaceFileKind",
    "WorkspaceFileScope",
    "WorkspaceProjection",
    "workspace_projection_fingerprint",
    "workspace_projection_payload",
]
