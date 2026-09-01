"""Workspace identity capability required by application conversations.

定义应用会话所需的工作区身份能力."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


class FilesystemAccessOperation(StrEnum):
    """Structured local filesystem operations understood by permission rules.

    结构化本地文件系统操作,供权限规则消费.
    """

    READ = "read"
    LIST = "list"
    SEARCH = "search"
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    MOVE = "move"


@dataclass(frozen=True, slots=True)
class FilesystemTargetRequest:
    """One raw structured target awaiting canonical local resolution.

    表示一个等待解析为本地规范目标的原始结构化路径.
    """

    requested_path: str
    operation: FilesystemAccessOperation
    must_exist: bool = False
    reject_link_like: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.requested_path, str) or not self.requested_path:
            raise ValueError("filesystem target path must be non-empty")
        if "\x00" in self.requested_path:
            raise ValueError("filesystem target path must not contain NUL")
        if not isinstance(self.operation, FilesystemAccessOperation):
            raise TypeError("filesystem target operation must be a FilesystemAccessOperation")
        if not isinstance(self.must_exist, bool) or not isinstance(self.reject_link_like, bool):
            raise TypeError("filesystem target flags must be boolean")


@dataclass(frozen=True, slots=True)
class FilesystemAccessTarget:
    """Immutable local filesystem identity shared by all security stages.

    ``requested_path`` is retained for diagnostics only.  Authorization and
    execution must use ``canonical_path`` and ``policy_path``.

    表示由所有安全阶段共享的不可变本地文件系统身份. ``requested_path`` 仅用于
    诊断;授权和执行必须使用 ``canonical_path`` 与 ``policy_path``.
    """

    requested_path: str
    canonical_path: Path
    owning_workspace_root: Path
    policy_path: str
    operation: FilesystemAccessOperation
    exists: bool
    is_primary_workspace: bool
    additional_workspace_root: Path | None = None
    contains_link_like_component: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.requested_path, str) or not self.requested_path:
            raise ValueError("filesystem target requested path must be non-empty")
        if not isinstance(self.canonical_path, Path):
            raise TypeError("filesystem target canonical path must be a Path")
        if not isinstance(self.owning_workspace_root, Path):
            raise TypeError("filesystem target workspace root must be a Path")
        if not isinstance(self.policy_path, str) or not self.policy_path:
            raise ValueError("filesystem target policy path must be non-empty")
        if not isinstance(self.operation, FilesystemAccessOperation):
            raise TypeError("filesystem target operation must be a FilesystemAccessOperation")
        for name in ("exists", "is_primary_workspace", "contains_link_like_component"):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"filesystem target {name} must be boolean")
        if self.is_primary_workspace and self.additional_workspace_root is not None:
            raise ValueError("primary filesystem target cannot have an additional root")
        if not self.is_primary_workspace and self.additional_workspace_root is None:
            raise ValueError("additional filesystem target must identify its additional root")


@dataclass(frozen=True, slots=True)
class FilesystemAccessPlan:
    """Ordered immutable targets for one structured filesystem tool call.

    The order is the tool grammar's extraction order, so a multi-target tool
    can execute exactly the targets that were preflighted.

    表示一次结构化文件系统工具调用的有序不可变目标集合. 顺序与工具语法提取
    顺序一致,使多目标工具只能执行已经完成预检的目标.
    """

    tool_name: str
    targets: tuple[FilesystemAccessTarget, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.tool_name, str) or not self.tool_name:
            raise ValueError("filesystem access plan tool name must be non-empty")
        object.__setattr__(self, "targets", tuple(self.targets))
        if not self.targets:
            raise ValueError("filesystem access plan must contain at least one target")
        if not all(isinstance(target, FilesystemAccessTarget) for target in self.targets):
            raise TypeError("filesystem access plan targets must be canonical")

    def target_at(self, index: int) -> FilesystemAccessTarget:
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise IndexError("filesystem target index is invalid")
        try:
            return self.targets[index]
        except IndexError:
            raise IndexError("filesystem target index is outside the prepared plan") from None


@runtime_checkable
class FilesystemTargetProvider(Protocol):
    """Prepare one canonical local target plan before permission evaluation.

    A delegated client filesystem returns ``None`` because its paths belong to
    a different authority and must not be resolved by the host filesystem.

    在权限评估前准备本地规范目标计划. 委托客户端文件系统返回 ``None``,因为其
    路径属于不同 authority,不得由宿主文件系统解析.
    """

    def prepare_filesystem_targets(
        self,
        arguments: Mapping[str, Any],
        context: Any,
        /,
    ) -> FilesystemAccessPlan | None: ...


class WorkspaceIdentity(Protocol):
    """Determine whether two paths identify the same workspace.

    确定两个路径是否指向同一个工作区."""

    def matches(
        self,
        recorded: str | Path,
        requested: str | Path,
        /,
    ) -> bool: ...


class WorkspacePathResolver(Protocol):
    """Resolve an existing path within a workspace boundary.

    解析工作区边界内的现有路径."""

    def resolve_existing(
        self,
        workspace: Path,
        requested: str,
        /,
    ) -> Path: ...


__all__ = [
    "FilesystemAccessOperation",
    "FilesystemAccessPlan",
    "FilesystemAccessTarget",
    "FilesystemTargetProvider",
    "FilesystemTargetRequest",
    "WorkspaceIdentity",
    "WorkspacePathResolver",
]
