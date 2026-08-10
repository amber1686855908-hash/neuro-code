"""Application contract for bounded workspace-change observation.

定义有界工作区变更观察的应用契约."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, NotRequired, Protocol, TypedDict, runtime_checkable

WorkspaceChangeStatus = Literal["created", "deleted", "modified"]
WorkspaceChangeHiddenReason = Literal[
    "sensitive",
    "large",
    "binary",
    "budget",
    "redacted",
]


class WorkspaceChangeCheckpoint:
    """Opaque checkpoint produced and consumed by a workspace observer.

    表示由工作区观察器生成并消费的不透明检查点."""

    __slots__ = ()


class WorkspaceChangeVisibleFileEventPayload(TypedDict):
    """Serialized change for a file whose contents can be shown safely.

    表示可以安全展示文件内容的序列化变更."""

    path: str
    status: WorkspaceChangeStatus
    additions: int
    deletions: int
    diff: str
    diff_truncated: bool
    hidden_reason: NotRequired[Literal["redacted"]]


class WorkspaceChangeHiddenFileEventPayload(TypedDict):
    """Serialized change for a file whose contents must remain hidden.

    表示必须隐藏文件内容的序列化变更."""

    path: str
    status: WorkspaceChangeStatus
    additions: int
    deletions: int
    hidden_reason: Literal["sensitive", "large", "binary", "budget"]


WorkspaceChangeFileEventPayload = (
    WorkspaceChangeVisibleFileEventPayload | WorkspaceChangeHiddenFileEventPayload
)


class WorkspaceChangeEventPayload(TypedDict):
    """The stable workspace-change payload embedded in terminal tool events.

    表示嵌入终端工具事件的稳定工作区变更载荷."""

    files: list[WorkspaceChangeFileEventPayload]
    omitted_files: int
    scan_limited: bool


@dataclass(frozen=True, slots=True)
class WorkspaceFileChange:
    path: str
    status: WorkspaceChangeStatus
    additions: int
    deletions: int
    diff: str | None = None
    diff_truncated: bool | None = None
    hidden_reason: WorkspaceChangeHiddenReason | None = None

    def to_event_payload(self) -> WorkspaceChangeFileEventPayload:
        if self.hidden_reason in {"sensitive", "large", "binary", "budget"}:
            return {
                "path": self.path,
                "status": self.status,
                "additions": self.additions,
                "deletions": self.deletions,
                "hidden_reason": self.hidden_reason,
            }

        if self.diff is None or self.diff_truncated is None:
            raise ValueError("visible workspace changes require diff details")

        payload: WorkspaceChangeVisibleFileEventPayload = {
            "path": self.path,
            "status": self.status,
            "additions": self.additions,
            "deletions": self.deletions,
            "diff": self.diff,
            "diff_truncated": self.diff_truncated,
        }
        if self.hidden_reason == "redacted":
            payload["hidden_reason"] = "redacted"
        return payload


@dataclass(frozen=True, slots=True)
class WorkspaceChangeReport:
    files: tuple[WorkspaceFileChange, ...]
    omitted_files: int
    scan_limited: bool

    @property
    def should_emit(self) -> bool:
        """Match the existing runtime policy for adding a change report to an event.

        遵循现有运行时策略,判断是否将变更报告加入事件."""

        return bool(self.files) or self.scan_limited

    def to_event_payload(self) -> WorkspaceChangeEventPayload:
        return {
            "files": [change.to_event_payload() for change in self.files],
            "omitted_files": self.omitted_files,
            "scan_limited": self.scan_limited,
        }


@dataclass(frozen=True, slots=True)
class WorkspaceDiffMove:
    """A bounded rename identity retained by the task workspace journal.

    由任务工作区日志保留的有界重命名身份.
    """

    old_path: str
    new_path: str

    def __post_init__(self) -> None:
        if not self.old_path or not self.new_path:
            raise ValueError("workspace diff move paths must be non-empty")
        if "\x00" in self.old_path or "\x00" in self.new_path:
            raise ValueError("workspace diff move paths must not contain NUL")
        if self.old_path == self.new_path:
            raise ValueError("workspace diff move paths must differ")


@dataclass(frozen=True, slots=True)
class WorkspaceDiffFile:
    """One bounded task-attributed file difference.

    一个有界且归因于当前任务的文件差异.
    """

    path: str
    status: WorkspaceChangeStatus
    additions: int
    deletions: int
    diff: str | None
    diff_truncated: bool
    hidden_reason: WorkspaceChangeHiddenReason | None = None

    def __post_init__(self) -> None:
        if not self.path or "\x00" in self.path:
            raise ValueError("workspace diff file path must be non-empty text")
        if self.additions < 0 or self.deletions < 0:
            raise ValueError("workspace diff counts must not be negative")
        if not isinstance(self.diff_truncated, bool):
            raise ValueError("workspace diff truncation must be boolean")
        if self.diff is None and self.hidden_reason is None:
            raise ValueError("hidden workspace diff files must declare a reason")


@dataclass(frozen=True, slots=True)
class WorkspaceDiffResult:
    """Structured, task-scoped workspace diff projection.

    结构化且按任务作用域生成的工作区差异投影.
    """

    files: tuple[WorkspaceDiffFile, ...]
    moved_files: tuple[WorkspaceDiffMove, ...]
    omitted_files: int
    scan_limited: bool
    truncated: bool
    structured_edits: bool
    workspace_observed: bool
    partial: bool
    unattributed_changes_detected: bool

    def __post_init__(self) -> None:
        if self.omitted_files < 0:
            raise ValueError("omitted workspace diff file count must not be negative")
        for value in (
            self.scan_limited,
            self.truncated,
            self.structured_edits,
            self.workspace_observed,
            self.partial,
            self.unattributed_changes_detected,
        ):
            if not isinstance(value, bool):
                raise ValueError("workspace diff flags must be boolean")


class WorkspaceChangeObserver(Protocol):
    """Capture and compare bounded workspace state around one tool invocation.

    在一次工具调用前后捕获并比较有界的工作区状态."""

    def capture(
        self,
        root: Path,
        /,
    ) -> WorkspaceChangeCheckpoint: ...

    def compare(
        self,
        before: WorkspaceChangeCheckpoint,
        after: WorkspaceChangeCheckpoint,
        *,
        explicit_redactions: tuple[str, ...],
    ) -> WorkspaceChangeReport: ...


class WorkspaceChangeJournal(Protocol):
    """Track first-write baselines for one in-memory agent task.

    为一个内存中的 Agent 任务记录首次写入前的基线.
    """

    def begin_task(self) -> None: ...

    def before_mutation(
        self,
        roots: tuple[Path, ...],
        *,
        tool_name: str,
        explicit_redactions: tuple[str, ...],
        target_paths: tuple[str, ...] = (),
    ) -> None: ...

    def after_mutation(
        self,
        roots: tuple[Path, ...],
        *,
        tool_name: str,
        mutation_metadata: Mapping[str, object] | None,
        explicit_redactions: tuple[str, ...],
        target_paths: tuple[str, ...] = (),
    ) -> None: ...

    def diff(
        self,
        roots: tuple[Path, ...],
        *,
        paths: tuple[str, ...],
        max_files: int,
        max_diff_bytes: int,
        context_lines: int,
        explicit_redactions: tuple[str, ...],
    ) -> WorkspaceDiffResult: ...


@runtime_checkable
class WorkspaceMutationJournalProjection(Protocol):
    """Project the last structured mutation without another full scan.

    在不再次进行全仓扫描的情况下投影最近一次结构化修改.
    """

    def last_change_report(
        self,
        *,
        explicit_redactions: tuple[str, ...],
    ) -> WorkspaceChangeReport | None: ...

    def record_external_observation(self, report: WorkspaceChangeReport) -> None: ...


@runtime_checkable
class WorkspaceMutationTargetProvider(Protocol):
    """Expose validated mutation targets for targeted baseline capture.

    为目标路径明确的结构化工具提供定向基线捕获路径.
    """

    def workspace_target_paths(self, arguments: Mapping[str, object]) -> tuple[str, ...]: ...


__all__ = [
    "WorkspaceChangeCheckpoint",
    "WorkspaceChangeEventPayload",
    "WorkspaceChangeFileEventPayload",
    "WorkspaceChangeHiddenFileEventPayload",
    "WorkspaceChangeHiddenReason",
    "WorkspaceChangeJournal",
    "WorkspaceChangeObserver",
    "WorkspaceChangeReport",
    "WorkspaceChangeStatus",
    "WorkspaceChangeVisibleFileEventPayload",
    "WorkspaceDiffFile",
    "WorkspaceDiffMove",
    "WorkspaceDiffResult",
    "WorkspaceFileChange",
    "WorkspaceMutationJournalProjection",
    "WorkspaceMutationTargetProvider",
]
