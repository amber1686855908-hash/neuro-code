"""Workspace identity capability required by application conversations.

定义应用会话所需的工作区身份能力."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


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


__all__ = ["WorkspaceIdentity", "WorkspacePathResolver"]
