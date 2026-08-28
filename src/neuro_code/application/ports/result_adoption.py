"""Application ports for bounded durable result adoption.

The adoption workflow owns verification and durable intent.  Filesystem
observation and mutation remain injected ports so the workflow cannot bypass
the normal workspace, permission, or sandbox boundaries.

定义有界持久化结果采纳的应用端口. 采纳工作流拥有验证与持久化意图;
文件系统观察和修改通过注入端口完成,工作流不能绕过既有工作区、权限或沙箱边界.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from neuro_code.domain.checkpoints import WorkspaceFileEntry, WorkspaceProjection
from neuro_code.domain.result_adoption import (
    ResultAdoptionOperation,
    ResultAdoptionPlan,
    ResultAdoptionState,
    ResultAdoptionTarget,
    ResultAdoptionTargetState,
)
from neuro_code.domain.worktree import WorktreeRepositoryIdentity, WorktreeSnapshot


class ResultAdoptionError(Exception):
    """Bounded fail-closed error at the result-adoption boundary."""

    def __init__(self, message: str, *, kind: str = "command_failed") -> None:
        self.kind = kind
        super().__init__(message[:1_000])


@dataclass(frozen=True, slots=True)
class ParentWorkspaceSnapshot:
    """Exact parent repository identity and bounded current projection."""

    repository: WorktreeRepositoryIdentity
    projection: WorkspaceProjection

    def __post_init__(self) -> None:
        if not isinstance(self.repository, WorktreeRepositoryIdentity):
            raise TypeError("parent workspace repository must be canonical")
        if not isinstance(self.projection, WorkspaceProjection):
            raise TypeError("parent workspace projection must be canonical")


@runtime_checkable
class ParentWorkspaceProjectionReader(Protocol):
    """Read the actual binding-owned parent workspace through infrastructure."""

    async def inspect(self, root: Path, /) -> ParentWorkspaceSnapshot: ...


@runtime_checkable
class ResultAdoptionWorktreePort(Protocol):
    """Inspect preserved managed worktrees through the application boundary."""

    async def initialize(self) -> None: ...

    async def inspect(self, worktree_id: str, /) -> WorktreeSnapshot: ...


@dataclass(frozen=True, slots=True)
class WorkspaceMutationRequest:
    """One exact regular-file mutation after the parent three-way check."""

    path: str
    operation: ResultAdoptionOperation
    expected: WorkspaceFileEntry | None
    desired: WorkspaceFileEntry | None

    def __post_init__(self) -> None:
        if not isinstance(self.path, str) or not self.path:
            raise ValueError("workspace mutation path must be non-empty")
        if not isinstance(self.operation, ResultAdoptionOperation):
            raise TypeError("workspace mutation operation must be canonical")
        if self.expected is not None and not isinstance(self.expected, WorkspaceFileEntry):
            raise TypeError("workspace mutation expected image must be canonical")
        if self.desired is not None and not isinstance(self.desired, WorkspaceFileEntry):
            raise TypeError("workspace mutation desired image must be canonical")
        if self.expected is not None and self.expected.path != self.path:
            raise ValueError("workspace mutation expected path does not match")
        if self.desired is not None and self.desired.path != self.path:
            raise ValueError("workspace mutation desired path does not match")


@dataclass(frozen=True, slots=True)
class WorkspaceMutationResult:
    """Bounded result returned by the canonical mutation execution seam."""

    path: str
    operation: ResultAdoptionOperation

    def __post_init__(self) -> None:
        if not isinstance(self.path, str) or not self.path:
            raise ValueError("workspace mutation result path must be non-empty")
        if not isinstance(self.operation, ResultAdoptionOperation):
            raise TypeError("workspace mutation result operation must be canonical")


@runtime_checkable
class WorkspaceMutationPort(Protocol):
    """Apply one exact mutation via the runtime permission/sandbox pipeline."""

    async def apply(
        self,
        request: WorkspaceMutationRequest,
        *,
        session_id: str,
    ) -> WorkspaceMutationResult: ...


@dataclass(frozen=True, slots=True)
class ResultAdoptionTargetRecord:
    target: ResultAdoptionTarget
    state: ResultAdoptionTargetState
    observed_fingerprint: str | None = None
    error_kind: str | None = None
    updated_at: datetime | None = None
    version: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.target, ResultAdoptionTarget):
            raise TypeError("result adoption target record target must be canonical")
        if not isinstance(self.state, ResultAdoptionTargetState):
            raise TypeError("result adoption target record state must be canonical")
        if self.observed_fingerprint is not None and (
            not isinstance(self.observed_fingerprint, str)
            or len(self.observed_fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in self.observed_fingerprint)
        ):
            raise ValueError("result adoption observed fingerprint is invalid")
        if self.error_kind is not None and (
            not isinstance(self.error_kind, str)
            or not self.error_kind
            or len(self.error_kind) > 256
        ):
            raise ValueError("result adoption target error kind is invalid")
        if self.updated_at is not None and (
            not isinstance(self.updated_at, datetime) or self.updated_at.tzinfo is None
        ):
            raise ValueError("result adoption target update time must be timezone-aware")
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 0:
            raise ValueError("result adoption target version must be non-negative")


@dataclass(frozen=True, slots=True)
class ResultAdoptionRecord:
    plan: ResultAdoptionPlan
    state: ResultAdoptionState
    owner_pid: int
    owner_token: str
    lease_expires_at: datetime
    created_at: datetime
    updated_at: datetime
    targets: tuple[ResultAdoptionTargetRecord, ...]
    error_kind: str | None = None
    version: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.plan, ResultAdoptionPlan):
            raise TypeError("result adoption record plan must be canonical")
        if not isinstance(self.state, ResultAdoptionState):
            raise TypeError("result adoption record state must be canonical")
        if (
            isinstance(self.owner_pid, bool)
            or not isinstance(self.owner_pid, int)
            or self.owner_pid <= 0
        ):
            raise ValueError("result adoption owner pid must be positive")
        if not isinstance(self.owner_token, str) or not self.owner_token:
            raise ValueError("result adoption owner token must be non-empty")
        for value, name in (
            (self.lease_expires_at, "lease expiry"),
            (self.created_at, "creation time"),
            (self.updated_at, "update time"),
        ):
            if not isinstance(value, datetime) or value.tzinfo is None:
                raise ValueError(f"result adoption {name} must be timezone-aware")
        if self.updated_at < self.created_at:
            raise ValueError("result adoption update time must not precede creation time")
        targets = tuple(self.targets)
        if len(targets) != len(self.plan.targets):
            raise ValueError("result adoption target records are incomplete")
        if not all(isinstance(value, ResultAdoptionTargetRecord) for value in targets):
            raise TypeError("result adoption target records must be canonical")
        if tuple(value.target.path for value in targets) != tuple(
            value.path for value in self.plan.targets
        ):
            raise ValueError("result adoption target records are out of order")
        if self.error_kind is not None and (
            not isinstance(self.error_kind, str) or not self.error_kind
        ):
            raise ValueError("result adoption error kind is invalid")
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 0:
            raise ValueError("result adoption version must be non-negative")
        object.__setattr__(self, "targets", targets)

    @property
    def adoption_id(self) -> str:
        return self.plan.adoption_id

    @property
    def plan_fingerprint(self) -> str:
        return self.plan.fingerprint

    @property
    def applied_paths(self) -> tuple[str, ...]:
        return tuple(
            record.target.path
            for record in self.targets
            if record.state is ResultAdoptionTargetState.APPLIED
        )


ProcessLivenessProbe = Callable[[int | None], bool]


@runtime_checkable
class ResultAdoptionStore(Protocol):
    """Durable plan, ownership, and per-target CAS state."""

    async def initialize(self) -> None: ...

    async def get_result_adoption(self, adoption_id: str, /) -> ResultAdoptionRecord | None: ...

    async def insert_result_adoption(
        self,
        plan: ResultAdoptionPlan,
        *,
        owner_pid: int,
        owner_token: str,
        now: datetime,
        lease_expires_at: datetime,
    ) -> ResultAdoptionRecord: ...

    async def claim_result_adoption(
        self,
        adoption_id: str,
        *,
        owner_pid: int,
        owner_token: str,
        now: datetime,
        lease_expires_at: datetime,
        owner_is_alive: ProcessLivenessProbe,
    ) -> ResultAdoptionRecord: ...

    async def transition_result_adoption(
        self,
        record: ResultAdoptionRecord,
        *,
        expected_version: int,
        expected_state: ResultAdoptionState,
    ) -> ResultAdoptionRecord: ...

    async def get_result_adoption_target(
        self,
        adoption_id: str,
        ordinal: int,
        /,
    ) -> ResultAdoptionTargetRecord | None: ...

    async def transition_result_adoption_target(
        self,
        record: ResultAdoptionTargetRecord,
        *,
        adoption_id: str,
        ordinal: int,
        owner_pid: int,
        owner_token: str,
        expected_version: int,
        expected_state: ResultAdoptionTargetState,
    ) -> ResultAdoptionTargetRecord: ...


__all__ = [
    "ParentWorkspaceProjectionReader",
    "ParentWorkspaceSnapshot",
    "ProcessLivenessProbe",
    "ResultAdoptionError",
    "ResultAdoptionRecord",
    "ResultAdoptionStore",
    "ResultAdoptionTargetRecord",
    "ResultAdoptionWorktreePort",
    "WorkspaceMutationPort",
    "WorkspaceMutationRequest",
    "WorkspaceMutationResult",
]
