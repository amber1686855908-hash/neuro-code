"""Lexical local file-URI helpers for already-authorized LSP paths."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import unquote, urlsplit


def _decode_uri_path(value: str) -> str | None:
    """Decode a URI path only after validating every percent escape."""

    for index, character in enumerate(value):
        if character != "%":
            continue
        if index + 2 >= len(value) or any(
            digit not in "0123456789abcdefABCDEF" for digit in value[index + 1 : index + 3]
        ):
            return None
    try:
        decoded = unquote(value, encoding="utf-8", errors="strict")
    except (UnicodeDecodeError, ValueError):
        return None
    return decoded if "\x00" not in decoded else None


def local_path_from_file_uri(uri: object) -> Path | None:
    """Parse a local file URI into a lexical absolute path.

    This helper intentionally does not resolve the path, inspect link-like
    components, or check workspace containment.  Callers handling untrusted
    server output must pass the returned path through the canonical filesystem
    target resolver before reading, authorizing, or displaying it.
    """

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
    decoded = _decode_uri_path(parsed.path)
    if not decoded:
        return None
    if os.name == "nt" and decoded.startswith("/") and len(decoded) >= 3 and decoded[2] == ":":
        decoded = decoded[1:]
    elif os.name != "nt" and decoded.startswith("//"):
        # A local POSIX file URI has one path-root slash.  Reject an encoded
        # authority-like prefix instead of relying on platform-specific
        # double-slash path semantics.
        return None
    candidate = Path(decoded)
    return candidate if candidate.is_absolute() else None


def path_from_file_uri(uri: object) -> Path | None:
    """Compatibility alias for the syntax-only local URI parser."""

    return local_path_from_file_uri(uri)


def file_uri_from_path(path: Path) -> str | None:
    """Create a file URI from an already canonical absolute path."""

    if not isinstance(path, Path) or not path.is_absolute():
        return None
    try:
        return path.as_uri()
    except ValueError:
        return None


def display_path(path: Path, workspace_root: Path) -> str:
    """Render only an already validated path for model-visible output."""

    try:
        return path.relative_to(workspace_root).as_posix() or "."
    except ValueError:
        return path.as_posix()


__all__ = [
    "display_path",
    "file_uri_from_path",
    "local_path_from_file_uri",
    "path_from_file_uri",
]
