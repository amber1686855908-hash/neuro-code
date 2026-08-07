"""Application contract for bounded workspace-change observation.

定义有界工作区变更观察的应用契约."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, NotRequired, Protocol, TypedDict

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


__all__ = [
    "WorkspaceChangeCheckpoint",
    "WorkspaceChangeEventPayload",
    "WorkspaceChangeFileEventPayload",
    "WorkspaceChangeHiddenFileEventPayload",
    "WorkspaceChangeHiddenReason",
    "WorkspaceChangeObserver",
    "WorkspaceChangeReport",
    "WorkspaceChangeStatus",
    "WorkspaceChangeVisibleFileEventPayload",
    "WorkspaceFileChange",
]
