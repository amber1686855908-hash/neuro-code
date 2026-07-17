from __future__ import annotations

from pathlib import Path

from neuro_code.errors import ToolError


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
