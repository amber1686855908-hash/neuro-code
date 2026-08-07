"""Compatibility facade for canonical workspace change infrastructure.

提供工作区变更基础设施的兼容门面,并转发到规范实现."""

from neuro_code.infrastructure.workspace.changes import (
    FilesystemWorkspaceChangeObserver,
    MultiRootWorkspaceChangeObserver,
    WorkspaceFileSnapshot,
    WorkspaceSnapshot,
    capture_workspace_snapshot,
    compare_workspace_snapshots,
)

__all__ = [
    "FilesystemWorkspaceChangeObserver",
    "MultiRootWorkspaceChangeObserver",
    "WorkspaceFileSnapshot",
    "WorkspaceSnapshot",
    "capture_workspace_snapshot",
    "compare_workspace_snapshots",
]
