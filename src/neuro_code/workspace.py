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


def _resolved_workspace_roots(
    cwd: Path,
    additional_workspace_roots: tuple[Path, ...],
) -> tuple[Path, ...]:
    """Return the primary root followed by independently accessible roots.

    Callers that accept untrusted directories must validate their shape and
    overlap before constructing a tool context.  The defensive overlap check
    here still keeps an accidentally malformed context from broadening the
    resolver's boundary.
    """

    root = cwd.expanduser().resolve()
    roots = [root]
    for additional in additional_workspace_roots:
        try:
            candidate = additional.expanduser().resolve()
        except (OSError, RuntimeError) as error:
            raise ToolError(f"cannot resolve additional workspace root: {error}") from error
        if any(
            candidate == existing
            or candidate.is_relative_to(existing)
            or existing.is_relative_to(candidate)
            for existing in roots
        ):
            raise ToolError("additional workspace roots must not overlap")
        roots.append(candidate)
    return tuple(roots)


def resolve_workspace_path(
    cwd: Path,
    requested: str,
    *,
    must_exist: bool = False,
    additional_workspace_roots: tuple[Path, ...] = (),
) -> Path:
    if not requested or "\x00" in requested:
        raise ToolError("path must be a non-empty filesystem path")
    roots = _resolved_workspace_roots(cwd, additional_workspace_roots)
    root = roots[0]
    candidate = Path(requested).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve(strict=must_exist)
    except OSError as error:
        raise ToolError(f"cannot resolve path {requested!r}: {error}") from error
    if not any(
        resolved == workspace_root or resolved.is_relative_to(workspace_root)
        for workspace_root in roots
    ):
        raise ToolError(f"path escapes the workspace: {requested!r}")
    return resolved


def is_additional_workspace_path(
    cwd: Path,
    path: Path,
    additional_workspace_roots: tuple[Path, ...],
) -> bool:
    """Return whether a resolved path belongs to an additional root."""

    roots = _resolved_workspace_roots(cwd, additional_workspace_roots)
    try:
        resolved = path.resolve(strict=False)
    except (OSError, RuntimeError):
        return False
    return any(resolved == root or resolved.is_relative_to(root) for root in roots[1:])


def workspace_display_path(
    cwd: Path,
    path: Path,
    additional_workspace_roots: tuple[Path, ...] = (),
) -> str:
    """Render primary-workspace paths relatively and extra-root paths absolutely."""

    roots = _resolved_workspace_roots(cwd, additional_workspace_roots)
    try:
        resolved = path.resolve(strict=False)
        return resolved.relative_to(roots[0]).as_posix()
    except (OSError, RuntimeError, ValueError):
        return str(path)


class FilesystemWorkspacePathResolver:
    """Filesystem-backed resolver for existing workspace paths."""

    def resolve_existing(
        self,
        workspace: Path,
        requested: str,
        /,
    ) -> Path:
        return resolve_workspace_path(workspace, requested, must_exist=True)
