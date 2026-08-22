"""Fail-closed local file URI projection for untrusted LSP results."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import unquote, urlsplit


def _contains_link_like_component(path: Path) -> bool:
    current = Path(path.anchor) if path.anchor else Path()
    parts = path.parts[1:] if path.anchor else path.parts
    for part in parts:
        current = current / part
        try:
            if current.is_symlink():
                return True
        except OSError:
            return True
    return False


def path_from_file_uri(
    uri: object,
    roots: tuple[Path, ...],
    *,
    reject_symlinks: bool = True,
) -> Path | None:
    """Return a canonical in-workspace path, or ``None`` for unsafe output."""

    if not isinstance(uri, str) or not uri or "\x00" in uri:
        return None
    try:
        parsed = urlsplit(uri)
    except ValueError:
        return None
    if parsed.scheme.casefold() != "file" or parsed.query or parsed.fragment:
        return None
    if parsed.netloc not in {"", "localhost"}:
        return None
    decoded = unquote(parsed.path)
    if not decoded or "\x00" in decoded:
        return None
    if os.name == "nt" and decoded.startswith("/") and len(decoded) >= 3 and decoded[2] == ":":
        decoded = decoded[1:]
    candidate = Path(decoded)
    if not candidate.is_absolute():
        return None
    try:
        canonical = candidate.resolve(strict=False)
        canonical_roots = tuple(root.expanduser().resolve(strict=False) for root in roots)
    except (OSError, RuntimeError):
        return None
    if reject_symlinks and _contains_link_like_component(candidate):
        return None
    if not any(canonical == root or canonical.is_relative_to(root) for root in canonical_roots):
        return None
    return canonical


def file_uri_from_path(path: Path) -> str | None:
    """Create a local file URI without accepting relative or link-like paths."""

    if not isinstance(path, Path) or not path.is_absolute():
        return None
    try:
        canonical = path.resolve(strict=False)
    except (OSError, RuntimeError):
        return None
    if _contains_link_like_component(path):
        return None
    try:
        return canonical.as_uri()
    except ValueError:
        return None


def display_path(path: Path, workspace_root: Path) -> str:
    """Render only an already validated path for model-visible output."""

    try:
        return path.relative_to(workspace_root).as_posix() or "."
    except ValueError:
        return path.as_posix()


__all__ = ["display_path", "file_uri_from_path", "path_from_file_uri"]
