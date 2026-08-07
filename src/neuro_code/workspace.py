"""Compatibility facade for canonical workspace path infrastructure.

提供工作区路径基础设施的兼容门面,并转发到规范实现."""

from neuro_code.infrastructure.workspace.paths import (
    FilesystemWorkspaceIdentity,
    FilesystemWorkspacePathResolver,
    is_additional_workspace_path,
    resolve_workspace_path,
    workspace_display_path,
    workspaces_match,
)

__all__ = [
    "FilesystemWorkspaceIdentity",
    "FilesystemWorkspacePathResolver",
    "is_additional_workspace_path",
    "resolve_workspace_path",
    "workspace_display_path",
    "workspaces_match",
]
