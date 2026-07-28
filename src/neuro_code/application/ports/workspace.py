"""Workspace identity capability required by application conversations."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class WorkspaceIdentity(Protocol):
    """Determine whether two paths identify the same workspace."""

    def matches(
        self,
        recorded: str | Path,
        requested: str | Path,
        /,
    ) -> bool: ...


class WorkspacePathResolver(Protocol):
    """Resolve an existing path within a workspace boundary."""

    def resolve_existing(
        self,
        workspace: Path,
        requested: str,
        /,
    ) -> Path: ...


__all__ = ["WorkspaceIdentity", "WorkspacePathResolver"]
