"""Application ports for the managed workspace checkpoint capability."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Protocol

from neuro_code.domain.checkpoints import (
    CheckpointCreateRequest,
    CheckpointId,
    CheckpointState,
    RollbackAttempt,
    RollbackAttemptId,
    RollbackState,
    WorkspaceCheckpoint,
    WorkspaceProjection,
)
from neuro_code.domain.worktree import WorktreeHandle

MAX_CHECKPOINT_FILES = 10_000
MAX_CHECKPOINT_UNTRACKED_FILES = 5_000
MAX_CHECKPOINT_TOTAL_BYTES = 64 * 1024 * 1024
MAX_CHECKPOINT_SINGLE_FILE_BYTES = 16 * 1024 * 1024
MAX_CHECKPOINT_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_CHECKPOINT_CAPTURE_SECONDS = 120.0


class CheckpointFailureKind(StrEnum):
    """String constants kept import-light for adapters and error messages."""

    NOT_AVAILABLE = "not_available"
    UNMANAGED = "unmanaged"
    FAILED_STATE = "failed_state"
    IDENTITY_MISMATCH = "identity_mismatch"
    UNSUPPORTED_WORKSPACE_STATE = "unsupported_workspace_state"
    CHECKPOINT_TOO_LARGE = "checkpoint_too_large"
    CHECKPOINT_CORRUPT = "checkpoint_corrupt"
    PATH_CONFLICT = "path_conflict"
    LOCKED = "locked"
    HEAD_MISMATCH = "head_mismatch"
    CONCURRENT_MODIFICATION = "concurrent_modification"
    ALREADY_ROLLING_BACK = "already_rolling_back"
    ROLLBACK_VERIFICATION_FAILED = "rollback_verification_failed"
    PROTOCOL = "protocol"
    COMMAND_FAILED = "command_failed"
    TIMEOUT = "timeout"


class WorkspaceCheckpointError(Exception):
    """Bounded typed failure for checkpoint capture and rollback."""

    def __init__(self, message: str, *, kind: str) -> None:
        if not isinstance(kind, str) or not kind:
            raise ValueError("checkpoint failure kind must be non-empty text")
        self.kind = kind
        super().__init__(message[:1_000])


class WorkspaceGitPort(Protocol):
    """Hardened Git seam for index metadata and the durable worktree lock."""

    async def index_path(self, path: Path, /) -> Path: ...

    async def index_entries(self, path: Path, /) -> bytes: ...

    async def nonignored_untracked_paths(self, path: Path, /) -> bytes: ...

    async def status_porcelain(self, path: Path, /) -> bytes: ...

    async def config_bool(self, path: Path, key: str, /) -> bool: ...

    async def read_index(self, path: Path, /) -> bytes: ...

    async def replace_index(self, path: Path, content: bytes, /) -> None: ...

    async def lock_worktree(self, path: Path, reason: str, /) -> None: ...

    async def unlock_worktree(self, path: Path, /) -> None: ...


class WorkspaceStatePort(Protocol):
    """Read and mutate only the projection bound to one managed handle."""

    async def inspect(self, handle: WorktreeHandle, /) -> WorkspaceProjection: ...

    async def restore(
        self,
        handle: WorktreeHandle,
        projection: WorkspaceProjection,
        /,
    ) -> None: ...


class CheckpointArtifactStore(Protocol):
    """Durable app-owned checkpoint artifact boundary."""

    async def initialize(self) -> None: ...

    def path_for(self, checkpoint_id: CheckpointId, /) -> Path: ...

    async def publish(
        self,
        checkpoint: WorkspaceCheckpoint,
        projection: WorkspaceProjection,
        /,
    ) -> WorkspaceCheckpoint: ...

    async def load(
        self,
        checkpoint: WorkspaceCheckpoint,
        /,
    ) -> WorkspaceProjection: ...

    async def recover(
        self,
        checkpoint: WorkspaceCheckpoint,
        /,
    ) -> WorkspaceCheckpoint: ...

    async def remove_temporary_capture(self, checkpoint_id: CheckpointId, /) -> None: ...


class WorkspaceCheckpointStore(Protocol):
    """Independent durable checkpoint and rollback store."""

    async def initialize(self) -> None: ...

    async def get(self, checkpoint_id: CheckpointId, /) -> WorkspaceCheckpoint | None: ...

    async def list(
        self,
        *,
        worktree_id: str | None = None,
        include_failed: bool = False,
    ) -> tuple[WorkspaceCheckpoint, ...]: ...

    async def insert_capturing(self, checkpoint: WorkspaceCheckpoint, /) -> WorkspaceCheckpoint: ...

    async def compare_and_transition_checkpoint(
        self,
        checkpoint: WorkspaceCheckpoint,
        *,
        expected_version: int,
        expected_state: CheckpointState,
    ) -> WorkspaceCheckpoint: ...

    async def get_attempt(self, attempt_id: RollbackAttemptId, /) -> RollbackAttempt | None: ...

    async def active_attempt(self, worktree_id: str, /) -> RollbackAttempt | None: ...

    async def list_active_attempts(self) -> tuple[RollbackAttempt, ...]: ...

    async def start_attempt(self, attempt: RollbackAttempt, /) -> RollbackAttempt: ...

    async def compare_and_transition_attempt(
        self,
        attempt: RollbackAttempt,
        *,
        expected_version: int,
        expected_state: RollbackState,
    ) -> RollbackAttempt: ...


class WorkspaceCheckpointApplication(Protocol):
    """Internal application capability; no model-facing tool is implied."""

    async def initialize(self) -> None: ...

    async def create(self, request: CheckpointCreateRequest) -> WorkspaceCheckpoint: ...

    async def inspect(self, handle: WorktreeHandle, /) -> WorkspaceProjection: ...

    async def get(self, checkpoint_id: CheckpointId, /) -> WorkspaceCheckpoint | None: ...

    async def load_projection(
        self,
        checkpoint_id: CheckpointId,
        /,
    ) -> WorkspaceProjection: ...

    async def rollback(
        self,
        checkpoint_id: CheckpointId,
        *,
        attempt_id: RollbackAttemptId | None = None,
    ) -> RollbackAttempt: ...

    async def reconcile(self) -> tuple[RollbackAttempt, ...]: ...


__all__ = [
    "MAX_CHECKPOINT_CAPTURE_SECONDS",
    "MAX_CHECKPOINT_FILES",
    "MAX_CHECKPOINT_MANIFEST_BYTES",
    "MAX_CHECKPOINT_SINGLE_FILE_BYTES",
    "MAX_CHECKPOINT_TOTAL_BYTES",
    "MAX_CHECKPOINT_UNTRACKED_FILES",
    "CheckpointArtifactStore",
    "CheckpointFailureKind",
    "WorkspaceCheckpointApplication",
    "WorkspaceCheckpointError",
    "WorkspaceCheckpointStore",
    "WorkspaceGitPort",
    "WorkspaceStatePort",
]
