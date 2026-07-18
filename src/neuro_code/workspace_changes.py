from __future__ import annotations

import difflib
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from neuro_code.redaction import redact_sensitive_text

_MAX_FILES = 4_000
_MAX_TEXT_FILE_BYTES = 256_000
_MAX_CAPTURED_BYTES = 8_000_000
_MAX_CHANGED_FILES = 20
_MAX_DIFF_LINES = 240
_MAX_DIFF_CHARACTERS = 24_000
_IGNORED_DIRECTORIES = frozenset(
    {
        ".git",
        ".hg",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "target",
        "venv",
    }
)
_SENSITIVE_NAMES = frozenset(
    {
        ".netrc",
        ".npmrc",
        "credentials.json",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
    }
)
_SENSITIVE_SUFFIXES = frozenset({".jks", ".key", ".p12", ".pfx", ".pem"})


@dataclass(frozen=True, slots=True)
class WorkspaceFileSnapshot:
    size: int
    modified_ns: int
    digest: str | None
    text: str | None
    hidden_reason: str | None = None


@dataclass(frozen=True, slots=True)
class WorkspaceSnapshot:
    files: dict[str, WorkspaceFileSnapshot]
    scan_limited: bool = False


def _sensitive_path(relative_path: Path) -> bool:
    name = relative_path.name.casefold()
    return (
        name == ".env"
        or name.startswith(".env.")
        or name in _SENSITIVE_NAMES
        or relative_path.suffix.casefold() in _SENSITIVE_SUFFIXES
    )


def capture_workspace_snapshot(root: Path) -> WorkspaceSnapshot:
    """Capture a bounded, read-only snapshot used to isolate one tool's file changes."""

    resolved_root = root.expanduser().resolve()
    files: dict[str, WorkspaceFileSnapshot] = {}
    captured_bytes = 0
    scan_limited = False

    for directory, directory_names, file_names in os.walk(resolved_root, followlinks=False):
        directory_names[:] = sorted(
            name
            for name in directory_names
            if name not in _IGNORED_DIRECTORIES and not (Path(directory) / name).is_symlink()
        )
        for file_name in sorted(file_names):
            if len(files) >= _MAX_FILES:
                scan_limited = True
                return WorkspaceSnapshot(files, scan_limited=True)
            path = Path(directory) / file_name
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                stat = path.stat()
                relative = path.relative_to(resolved_root)
            except (OSError, ValueError):
                continue
            key = relative.as_posix()
            if _sensitive_path(relative):
                files[key] = WorkspaceFileSnapshot(
                    stat.st_size,
                    stat.st_mtime_ns,
                    None,
                    None,
                    "sensitive",
                )
                continue
            if stat.st_size > _MAX_TEXT_FILE_BYTES:
                files[key] = WorkspaceFileSnapshot(
                    stat.st_size,
                    stat.st_mtime_ns,
                    None,
                    None,
                    "large",
                )
                continue
            if captured_bytes + stat.st_size > _MAX_CAPTURED_BYTES:
                scan_limited = True
                files[key] = WorkspaceFileSnapshot(
                    stat.st_size,
                    stat.st_mtime_ns,
                    None,
                    None,
                    "budget",
                )
                continue
            try:
                content = path.read_bytes()
            except OSError:
                continue
            captured_bytes += len(content)
            digest = hashlib.sha256(content).hexdigest()
            try:
                text = content.decode("utf-8")
                hidden_reason = None
            except UnicodeDecodeError:
                text = None
                hidden_reason = "binary"
            files[key] = WorkspaceFileSnapshot(
                stat.st_size,
                stat.st_mtime_ns,
                digest,
                text,
                hidden_reason,
            )
    return WorkspaceSnapshot(files, scan_limited=scan_limited)


def _file_changed(before: WorkspaceFileSnapshot, after: WorkspaceFileSnapshot) -> bool:
    if before.digest is not None and after.digest is not None:
        return before.digest != after.digest
    return (before.size, before.modified_ns) != (after.size, after.modified_ns)


def _bounded_diff(
    before_text: str,
    after_text: str,
    *,
    path: str,
    status: str,
    explicit_redactions: tuple[str, ...],
) -> tuple[str, int, int, bool, bool]:
    safe_before = redact_sensitive_text(before_text, explicit_values=explicit_redactions)
    safe_after = redact_sensitive_text(after_text, explicit_values=explicit_redactions)
    content_redacted = safe_before != before_text or safe_after != after_text
    from_file = "/dev/null" if status == "created" else f"a/{path}"
    to_file = "/dev/null" if status == "deleted" else f"b/{path}"
    raw_lines = list(
        difflib.unified_diff(
            safe_before.splitlines(),
            safe_after.splitlines(),
            fromfile=from_file,
            tofile=to_file,
            lineterm="",
            n=3,
        )
    )
    additions = sum(line.startswith("+") and not line.startswith("+++") for line in raw_lines)
    deletions = sum(line.startswith("-") and not line.startswith("---") for line in raw_lines)
    selected: list[str] = []
    characters = 0
    truncated = False
    for line in raw_lines:
        projected = characters + len(line) + (1 if selected else 0)
        if len(selected) >= _MAX_DIFF_LINES or projected > _MAX_DIFF_CHARACTERS:
            truncated = True
            break
        selected.append(line)
        characters = projected
    return "\n".join(selected), additions, deletions, truncated, content_redacted


def compare_workspace_snapshots(
    before: WorkspaceSnapshot,
    after: WorkspaceSnapshot,
    *,
    explicit_redactions: tuple[str, ...] = (),
) -> dict[str, object]:
    """Return a JSON-safe, redacted and bounded change report for one tool call."""

    changed_paths: list[tuple[str, str]] = []
    for path in sorted(before.files.keys() | after.files.keys()):
        old = before.files.get(path)
        new = after.files.get(path)
        if old is None:
            changed_paths.append((path, "created"))
        elif new is None:
            changed_paths.append((path, "deleted"))
        elif _file_changed(old, new):
            changed_paths.append((path, "modified"))

    omitted_files = max(0, len(changed_paths) - _MAX_CHANGED_FILES)
    files: list[dict[str, object]] = []
    for path, status in changed_paths[:_MAX_CHANGED_FILES]:
        old = before.files.get(path)
        new = after.files.get(path)
        hidden_reason = (old.hidden_reason if old is not None else None) or (
            new.hidden_reason if new is not None else None
        )
        before_text = old.text if old is not None and old.text is not None else ""
        after_text = new.text if new is not None and new.text is not None else ""
        detail: dict[str, object] = {"path": path, "status": status}
        if hidden_reason is None:
            diff, additions, deletions, truncated, content_redacted = _bounded_diff(
                before_text,
                after_text,
                path=path,
                status=status,
                explicit_redactions=explicit_redactions,
            )
            detail.update(
                {
                    "additions": additions,
                    "deletions": deletions,
                    "diff": diff,
                    "diff_truncated": truncated,
                }
            )
            if content_redacted:
                detail["hidden_reason"] = "redacted"
        else:
            detail.update({"additions": 0, "deletions": 0, "hidden_reason": hidden_reason})
        files.append(detail)
    return {
        "files": files,
        "omitted_files": omitted_files,
        "scan_limited": before.scan_limited or after.scan_limited,
    }


__all__ = [
    "WorkspaceFileSnapshot",
    "WorkspaceSnapshot",
    "capture_workspace_snapshot",
    "compare_workspace_snapshots",
]
