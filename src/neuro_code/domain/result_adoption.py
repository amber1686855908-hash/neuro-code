"""Immutable values for bounded durable result adoption.

Result adoption is deliberately a separate capability from worker execution.
The values in this module bind a completed worker projection to an exact parent
workspace plan; they do not perform filesystem I/O.

定义有界持久化结果采纳的不可变领域值. 结果采纳与 worker 执行刻意分离;
本模块只把已完成 worker 投影绑定到精确的父工作区计划,不执行文件系统 I/O.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath

from neuro_code.domain.checkpoints import (
    CheckpointId,
    WorkspaceFileEntry,
    WorkspaceFileKind,
    WorkspaceFileScope,
)
from neuro_code.domain.worktree import WorktreeId, WorktreeRepositoryIdentity

MAX_RESULT_ADOPTION_ID_BYTES = 128
MAX_RESULT_ADOPTION_ERROR_BYTES = 1_000
MAX_RESULT_ADOPTION_SOURCES = 8
MAX_RESULT_ADOPTION_TARGETS = 64
MAX_RESULT_ADOPTION_TOTAL_BYTES = 32 * 1024 * 1024
MAX_RESULT_ADOPTION_FILE_BYTES = 8 * 1024 * 1024
MAX_RESULT_ADOPTION_PATH_BYTES = 4 * 1024
MAX_RESULT_ADOPTION_LEASE_SECONDS = 300.0

_ID_PATTERN = re.compile(r"^adopt-[a-z0-9][a-z0-9_-]{0,124}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40,64}$")


def _safe_identifier(
    value: str, *, field_name: str, limit: int = MAX_RESULT_ADOPTION_ID_BYTES
) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\x00" in value
        or len(value.encode("utf-8")) > limit
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{field_name} must be a bounded safe identifier")
    return value


def _digest(value: str, *, field_name: str) -> str:
    normalized = _safe_identifier(value, field_name=field_name, limit=64).casefold()
    if _SHA256_PATTERN.fullmatch(normalized) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
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


def _relative_path(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or "\\" in value
        or value.startswith("/")
        or len(value.encode("utf-8")) > MAX_RESULT_ADOPTION_PATH_BYTES
    ):
        raise ValueError("result adoption path must be a bounded relative POSIX path")
    parts = PurePosixPath(value).parts
    if not parts or any(part in {"", ".", ".."} for part in parts) or ":" in parts[0]:
        raise ValueError("result adoption path must not contain traversal components")
    return "/".join(parts)


def _entry_payload(entry: WorkspaceFileEntry | None) -> dict[str, object] | None:
    if entry is None:
        return None
    payload: dict[str, object] = {
        "path": entry.path,
        "scope": entry.scope.value,
        "present": entry.present,
        "kind": entry.kind.value,
        "mode": entry.mode,
    }
    if entry.content is not None:
        payload["content_b64"] = base64.b64encode(entry.content).decode("ascii")
    if entry.link_target is not None:
        payload["link_target"] = entry.link_target
    return payload


def _entry_from_payload(raw: object) -> WorkspaceFileEntry | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("result adoption workspace image is invalid")
    path_raw = raw.get("path")
    present_raw = raw.get("present")
    mode_raw = raw.get("mode")
    if (
        not isinstance(path_raw, str)
        or not isinstance(present_raw, bool)
        or isinstance(mode_raw, bool)
        or not isinstance(mode_raw, int)
    ):
        raise ValueError("result adoption workspace image fields are invalid")
    content_raw = raw.get("content_b64")
    content: bytes | None = None
    if content_raw is not None:
        if not isinstance(content_raw, str):
            raise ValueError("result adoption content encoding is invalid")
        try:
            content = base64.b64decode(content_raw.encode("ascii"), validate=True)
        except (ValueError, UnicodeError) as error:
            raise ValueError("result adoption content encoding is invalid") from error
    scope_raw = raw.get("scope")
    kind_raw = raw.get("kind")
    if not isinstance(scope_raw, str) or not isinstance(kind_raw, str):
        raise ValueError("result adoption workspace image identity is invalid")
    return WorkspaceFileEntry(
        path=path_raw,
        scope=WorkspaceFileScope(scope_raw),
        present=present_raw,
        kind=WorkspaceFileKind(kind_raw),
        mode=mode_raw,
        content=content,
        link_target=(str(raw["link_target"]) if raw.get("link_target") is not None else None),
    )


def _repository_from_payload(raw: object) -> WorktreeRepositoryIdentity:
    if not isinstance(raw, dict):
        raise ValueError("result adoption repository identity is invalid")
    common_dir = raw.get("common_dir")
    source_worktree = raw.get("source_worktree")
    git_dir = raw.get("git_dir")
    head_sha = raw.get("head_sha")
    if not all(
        isinstance(value, str) for value in (common_dir, source_worktree, git_dir, head_sha)
    ):
        raise ValueError("result adoption repository identity fields are invalid")
    assert isinstance(common_dir, str)
    assert isinstance(source_worktree, str)
    assert isinstance(git_dir, str)
    assert isinstance(head_sha, str)
    return WorktreeRepositoryIdentity(
        common_dir=Path(common_dir),
        source_worktree=Path(source_worktree),
        git_dir=Path(git_dir),
        head_sha=head_sha,
    )


def workspace_entry_fingerprint(entry: WorkspaceFileEntry | None) -> str:
    """Fingerprint one exact path image without depending on a worker handle."""

    payload = _entry_payload(entry)
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class ResultAdoptionOperation(StrEnum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"


class ResultAdoptionState(StrEnum):
    CLAIMED = "claimed"
    VERIFIED = "verified"
    APPLYING = "applying"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    CONFLICT = "conflict"
    FAILED = "failed"
    INDETERMINATE = "indeterminate"

    @property
    def terminal(self) -> bool:
        return self in {
            ResultAdoptionState.COMPLETED,
            ResultAdoptionState.CONFLICT,
            ResultAdoptionState.FAILED,
            ResultAdoptionState.INDETERMINATE,
        }


class ResultAdoptionTargetState(StrEnum):
    NOT_STARTED = "not_started"
    APPLYING = "applying"
    RETRYABLE = "retryable"
    APPLIED = "applied"
    CONFLICT = "conflict"
    FAILED = "failed"
    INDETERMINATE = "indeterminate"

    @property
    def terminal(self) -> bool:
        return self in {
            ResultAdoptionTargetState.APPLIED,
            ResultAdoptionTargetState.CONFLICT,
            ResultAdoptionTargetState.FAILED,
            ResultAdoptionTargetState.INDETERMINATE,
        }


@dataclass(frozen=True, slots=True)
class ResultAdoptionSource:
    """Exact durable identity of one completed writable DAG source."""

    node_id: str
    parent_task_id: str
    child_session_id: str
    lease_id: str
    worktree_id: WorktreeId
    baseline_checkpoint_id: CheckpointId
    base_commit_sha: str
    final_workspace_fingerprint: str
    capability_fingerprint: str
    grant_fingerprint: str
    parent_repository: WorktreeRepositoryIdentity

    def __post_init__(self) -> None:
        for value, name in (
            (self.node_id, "result adoption source node id"),
            (self.parent_task_id, "result adoption source parent task id"),
            (self.child_session_id, "result adoption source child session id"),
            (self.lease_id, "result adoption source lease id"),
            (self.base_commit_sha, "result adoption source base commit"),
        ):
            _safe_identifier(value, field_name=name, limit=512)
        if not isinstance(self.worktree_id, WorktreeId):
            raise TypeError("result adoption source worktree id must be canonical")
        if not isinstance(self.baseline_checkpoint_id, CheckpointId):
            raise TypeError("result adoption source checkpoint id must be canonical")
        if _COMMIT_PATTERN.fullmatch(self.base_commit_sha.casefold()) is None:
            raise ValueError("result adoption source base commit must be a Git SHA")
        for value, name in (
            (self.final_workspace_fingerprint, "result adoption source final fingerprint"),
            (self.capability_fingerprint, "result adoption source capability fingerprint"),
            (self.grant_fingerprint, "result adoption source grant fingerprint"),
        ):
            _digest(value, field_name=name)
        if not isinstance(self.parent_repository, WorktreeRepositoryIdentity):
            raise TypeError("result adoption source repository must be canonical")

    def to_dict(self) -> dict[str, object]:
        return {
            "node_id": self.node_id,
            "parent_task_id": self.parent_task_id,
            "child_session_id": self.child_session_id,
            "lease_id": self.lease_id,
            "worktree_id": self.worktree_id.value,
            "baseline_checkpoint_id": self.baseline_checkpoint_id.value,
            "base_commit_sha": self.base_commit_sha,
            "final_workspace_fingerprint": self.final_workspace_fingerprint,
            "capability_fingerprint": self.capability_fingerprint,
            "grant_fingerprint": self.grant_fingerprint,
            "parent_repository": {
                "common_dir": str(self.parent_repository.common_dir),
                "source_worktree": str(self.parent_repository.source_worktree),
                "git_dir": str(self.parent_repository.git_dir),
                "head_sha": self.parent_repository.head_sha,
            },
        }

    @classmethod
    def from_dict(cls, raw: object) -> ResultAdoptionSource:
        if not isinstance(raw, dict):
            raise ValueError("result adoption source is invalid")
        return cls(
            node_id=str(raw.get("node_id")),
            parent_task_id=str(raw.get("parent_task_id")),
            child_session_id=str(raw.get("child_session_id")),
            lease_id=str(raw.get("lease_id")),
            worktree_id=WorktreeId(str(raw.get("worktree_id"))),
            baseline_checkpoint_id=CheckpointId(str(raw.get("baseline_checkpoint_id"))),
            base_commit_sha=str(raw.get("base_commit_sha")),
            final_workspace_fingerprint=str(raw.get("final_workspace_fingerprint")),
            capability_fingerprint=str(raw.get("capability_fingerprint")),
            grant_fingerprint=str(raw.get("grant_fingerprint")),
            parent_repository=_repository_from_payload(raw.get("parent_repository")),
        )


@dataclass(frozen=True, slots=True)
class ResultAdoptionTarget:
    """One exact three-way operation generated from baseline and desired images."""

    path: str
    operation: ResultAdoptionOperation
    baseline: WorkspaceFileEntry | None
    desired: WorkspaceFileEntry | None

    def __post_init__(self) -> None:
        normalized = _relative_path(self.path)
        object.__setattr__(self, "path", normalized)
        if not isinstance(self.operation, ResultAdoptionOperation):
            raise TypeError("result adoption operation must be canonical")
        for entry, name in ((self.baseline, "baseline"), (self.desired, "desired")):
            if entry is not None:
                if not isinstance(entry, WorkspaceFileEntry) or not entry.present:
                    raise ValueError(f"result adoption {name} image must be present")
                if entry.path != normalized:
                    raise ValueError(f"result adoption {name} path does not match target")
        if (
            self.baseline is not None
            and self.desired is not None
            and self.baseline.scope is not self.desired.scope
        ):
            raise ValueError("result adoption target scope cannot change")
        expected = {
            ResultAdoptionOperation.CREATE: (False, True),
            ResultAdoptionOperation.UPDATE: (True, True),
            ResultAdoptionOperation.DELETE: (True, False),
        }[self.operation]
        if (self.baseline is not None, self.desired is not None) != expected:
            raise ValueError("result adoption operation does not match its three-way images")

    @property
    def pre_image_fingerprint(self) -> str:
        return workspace_entry_fingerprint(self.baseline)

    @property
    def desired_fingerprint(self) -> str:
        return workspace_entry_fingerprint(self.desired)

    @property
    def byte_count(self) -> int:
        return sum(
            len(entry.content) if entry is not None and entry.content is not None else 0
            for entry in (self.baseline, self.desired)
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "operation": self.operation.value,
            "baseline": _entry_payload(self.baseline),
            "desired": _entry_payload(self.desired),
            "pre_image_fingerprint": self.pre_image_fingerprint,
            "desired_fingerprint": self.desired_fingerprint,
        }

    @classmethod
    def from_dict(cls, raw: object) -> ResultAdoptionTarget:
        if not isinstance(raw, dict):
            raise ValueError("result adoption target is invalid")
        operation = raw.get("operation")
        if not isinstance(operation, str):
            raise ValueError("result adoption target operation is invalid")
        return cls(
            path=str(raw.get("path")),
            operation=ResultAdoptionOperation(operation),
            baseline=_entry_from_payload(raw.get("baseline")),
            desired=_entry_from_payload(raw.get("desired")),
        )


@dataclass(frozen=True, slots=True)
class ResultAdoptionPlan:
    """Application-generated immutable binding for one adoption attempt."""

    adoption_id: str
    parent_session_id: str
    parent_workspace_root: Path
    parent_repository: WorktreeRepositoryIdentity
    parent_head_sha: str
    swarm_run_id: str
    dag_id: str
    dag_generation: int
    dag_definition_fingerprint: str
    sources: tuple[ResultAdoptionSource, ...]
    targets: tuple[ResultAdoptionTarget, ...]
    created_at: datetime

    def __post_init__(self) -> None:
        if (
            _ID_PATTERN.fullmatch(_safe_identifier(self.adoption_id, field_name="adoption id"))
            is None
        ):
            raise ValueError("adoption id must use the adopt- prefix")
        for value, name in (
            (self.parent_session_id, "adoption parent session id"),
            (self.parent_head_sha, "adoption parent HEAD"),
            (self.swarm_run_id, "adoption swarm run id"),
            (self.dag_id, "adoption DAG id"),
        ):
            _safe_identifier(value, field_name=name, limit=512)
        object.__setattr__(
            self,
            "parent_workspace_root",
            _canonical_path(
                self.parent_workspace_root, field_name="adoption parent workspace root"
            ),
        )
        if not isinstance(self.parent_repository, WorktreeRepositoryIdentity):
            raise TypeError("adoption parent repository must be canonical")
        if self.parent_workspace_root != self.parent_repository.source_worktree:
            raise ValueError("adoption parent workspace must be the repository source root")
        if self.parent_head_sha != self.parent_repository.head_sha:
            raise ValueError("adoption parent HEAD does not match repository identity")
        if (
            isinstance(self.dag_generation, bool)
            or not isinstance(self.dag_generation, int)
            or self.dag_generation < 0
        ):
            raise ValueError("adoption DAG generation must be non-negative")
        _digest(self.dag_definition_fingerprint, field_name="adoption DAG definition fingerprint")
        sources = tuple(self.sources)
        targets = tuple(self.targets)
        if not 1 <= len(sources) <= MAX_RESULT_ADOPTION_SOURCES:
            raise ValueError(
                f"adoption source count must be between 1 and {MAX_RESULT_ADOPTION_SOURCES}"
            )
        if len(targets) > MAX_RESULT_ADOPTION_TARGETS:
            raise ValueError(f"adoption target count exceeds {MAX_RESULT_ADOPTION_TARGETS}")
        if not all(isinstance(source, ResultAdoptionSource) for source in sources):
            raise TypeError("adoption sources must be canonical")
        if not all(isinstance(target, ResultAdoptionTarget) for target in targets):
            raise TypeError("adoption targets must be canonical")
        if len({source.node_id for source in sources}) != len(sources):
            raise ValueError("adoption source nodes must be unique")
        if len({target.path for target in targets}) != len(targets):
            raise ValueError("adoption target paths must be unique")
        if any(source.parent_repository != self.parent_repository for source in sources):
            raise ValueError("adoption sources do not share the exact parent repository")
        if sum(target.byte_count for target in targets) > MAX_RESULT_ADOPTION_TOTAL_BYTES:
            raise ValueError("adoption target content exceeds the bounded total size")
        if any(
            entry is not None
            and entry.content is not None
            and len(entry.content) > MAX_RESULT_ADOPTION_FILE_BYTES
            for target in targets
            for entry in (target.baseline, target.desired)
        ):
            raise ValueError("adoption target content exceeds the bounded file size")
        if not isinstance(self.created_at, datetime) or self.created_at.tzinfo is None:
            raise ValueError("adoption plan creation time must be timezone-aware")
        object.__setattr__(self, "sources", sources)
        object.__setattr__(self, "targets", targets)
        object.__setattr__(self, "created_at", self.created_at.astimezone(UTC))

    @classmethod
    def new_id(cls) -> str:
        return f"adopt-{uuid.uuid4().hex}"

    def to_dict(self) -> dict[str, object]:
        return {
            "adoption_id": self.adoption_id,
            "parent_session_id": self.parent_session_id,
            "parent_workspace_root": str(self.parent_workspace_root),
            "parent_repository": {
                "common_dir": str(self.parent_repository.common_dir),
                "source_worktree": str(self.parent_repository.source_worktree),
                "git_dir": str(self.parent_repository.git_dir),
                "head_sha": self.parent_repository.head_sha,
            },
            "parent_head_sha": self.parent_head_sha,
            "swarm_run_id": self.swarm_run_id,
            "dag_id": self.dag_id,
            "dag_generation": self.dag_generation,
            "dag_definition_fingerprint": self.dag_definition_fingerprint,
            "sources": [source.to_dict() for source in self.sources],
            "targets": [target.to_dict() for target in self.targets],
            "created_at": self.created_at.isoformat(),
        }

    @property
    def fingerprint(self) -> str:
        encoded = json.dumps(
            self.to_dict(), ensure_ascii=True, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(cls, raw: object) -> ResultAdoptionPlan:
        if not isinstance(raw, dict):
            raise ValueError("result adoption plan is invalid")
        sources_raw = raw.get("sources")
        targets_raw = raw.get("targets")
        if not isinstance(sources_raw, list) or not isinstance(targets_raw, list):
            raise ValueError("result adoption plan source/target lists are invalid")
        dag_generation = raw.get("dag_generation")
        if isinstance(dag_generation, bool) or not isinstance(dag_generation, int):
            raise ValueError("result adoption DAG generation is invalid")
        repository = _repository_from_payload(raw.get("parent_repository"))
        return cls(
            adoption_id=str(raw.get("adoption_id")),
            parent_session_id=str(raw.get("parent_session_id")),
            parent_workspace_root=Path(str(raw.get("parent_workspace_root"))),
            parent_repository=repository,
            parent_head_sha=str(raw.get("parent_head_sha")),
            swarm_run_id=str(raw.get("swarm_run_id")),
            dag_id=str(raw.get("dag_id")),
            dag_generation=dag_generation,
            dag_definition_fingerprint=str(raw.get("dag_definition_fingerprint")),
            sources=tuple(ResultAdoptionSource.from_dict(value) for value in sources_raw),
            targets=tuple(ResultAdoptionTarget.from_dict(value) for value in targets_raw),
            created_at=datetime.fromisoformat(str(raw.get("created_at"))),
        )


@dataclass(frozen=True, slots=True)
class ResultAdoptionRequest:
    """Explicit internal request; the service generates the plan from durable evidence."""

    adoption_id: str
    swarm_run_id: str

    def __post_init__(self) -> None:
        if (
            _ID_PATTERN.fullmatch(_safe_identifier(self.adoption_id, field_name="adoption id"))
            is None
        ):
            raise ValueError("adoption id must use the adopt- prefix")
        _safe_identifier(self.swarm_run_id, field_name="adoption swarm run id", limit=512)


__all__ = [
    "MAX_RESULT_ADOPTION_ERROR_BYTES",
    "MAX_RESULT_ADOPTION_FILE_BYTES",
    "MAX_RESULT_ADOPTION_LEASE_SECONDS",
    "MAX_RESULT_ADOPTION_PATH_BYTES",
    "MAX_RESULT_ADOPTION_SOURCES",
    "MAX_RESULT_ADOPTION_TARGETS",
    "MAX_RESULT_ADOPTION_TOTAL_BYTES",
    "ResultAdoptionOperation",
    "ResultAdoptionPlan",
    "ResultAdoptionRequest",
    "ResultAdoptionSource",
    "ResultAdoptionState",
    "ResultAdoptionTarget",
    "ResultAdoptionTargetState",
    "workspace_entry_fingerprint",
]
