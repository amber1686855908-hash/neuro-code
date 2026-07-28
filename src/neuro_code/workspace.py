from __future__ import annotations

import os
from pathlib import Path

from neuro_code.shared.errors import ToolError


def workspaces_match(recorded: str | Path, requested: str | Path) -> bool:
    """Return whether two workspace spellings identify the same filesystem location."""

    try:
        recorded_path = Path(recorded).expanduser()
        requested_path = Path(requested).expanduser()
    except RuntimeError:
        return False

    try:
        return recorded_path.samefile(requested_path)
    except (OSError, ValueError):
        pass

    try:
        recorded_resolved = recorded_path.resolve(strict=False)
        requested_resolved = requested_path.resolve(strict=False)
    except (OSError, RuntimeError):
        return False
    return os.path.normcase(os.fspath(recorded_resolved)) == os.path.normcase(
        os.fspath(requested_resolved)
    )


class FilesystemWorkspaceIdentity:
    """Filesystem-backed workspace identity implementation."""

    def matches(
        self,
        recorded: str | Path,
        requested: str | Path,
        /,
    ) -> bool:
        return workspaces_match(recorded, requested)


def resolve_workspace_path(cwd: Path, requested: str, *, must_exist: bool = False) -> Path:
    if not requested or "\x00" in requested:
        raise ToolError("path must be a non-empty filesystem path")
    root = cwd.expanduser().resolve()
    candidate = Path(requested).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve(strict=must_exist)
    except OSError as error:
        raise ToolError(f"cannot resolve path {requested!r}: {error}") from error
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise ToolError(f"path escapes the workspace: {requested!r}") from error
    return resolved


class FilesystemWorkspacePathResolver:
    """Filesystem-backed resolver for existing workspace paths."""

    def resolve_existing(
        self,
        workspace: Path,
        requested: str,
        /,
    ) -> Path:
        return resolve_workspace_path(workspace, requested, must_exist=True)
