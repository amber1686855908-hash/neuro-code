from __future__ import annotations

import difflib
import hashlib
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from neuro_code.application.ports.workspace_changes import (
    WorkspaceChangeCheckpoint,
    WorkspaceChangeHiddenReason,
    WorkspaceChangeObserver,
    WorkspaceChangeReport,
    WorkspaceChangeStatus,
    WorkspaceFileChange,
)
from neuro_code.shared.redaction import redact_sensitive_text

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


class _FilesystemWorkspaceChangeCheckpoint(WorkspaceChangeCheckpoint):
    """Keep filesystem snapshots private to the filesystem observer."""

    __slots__ = ("_snapshot",)

    def __init__(self, snapshot: WorkspaceSnapshot) -> None:
        self._snapshot = snapshot


class FilesystemWorkspaceChangeObserver:
    """Adapt bounded filesystem snapshots to the application observer contract."""

    def capture(self, root: Path, /) -> WorkspaceChangeCheckpoint:
        return _FilesystemWorkspaceChangeCheckpoint(capture_workspace_snapshot(root))

    def compare(
        self,
        before: WorkspaceChangeCheckpoint,
        after: WorkspaceChangeCheckpoint,
        *,
        explicit_redactions: tuple[str, ...],
    ) -> WorkspaceChangeReport:
        before_checkpoint = self._filesystem_checkpoint(before)
        after_checkpoint = self._filesystem_checkpoint(after)
        comparison = compare_workspace_snapshots(
            before_checkpoint._snapshot,
            after_checkpoint._snapshot,
            explicit_redactions=explicit_redactions,
        )
        return _workspace_change_report_from_comparison(comparison)

    @staticmethod
    def _filesystem_checkpoint(
        checkpoint: WorkspaceChangeCheckpoint,
    ) -> _FilesystemWorkspaceChangeCheckpoint:
        if not isinstance(checkpoint, _FilesystemWorkspaceChangeCheckpoint):
            raise TypeError("workspace checkpoint belongs to a different observer")
        return checkpoint


class _MultiRootWorkspaceChangeCheckpoint(WorkspaceChangeCheckpoint):
    """Opaque checkpoints for one primary and bounded additional roots."""

    __slots__ = ("checkpoints",)

    def __init__(self, checkpoints: tuple[WorkspaceChangeCheckpoint, ...]) -> None:
        self.checkpoints = checkpoints


class MultiRootWorkspaceChangeObserver:
    """Add bounded extra-root snapshots without changing the application port.

    The primary root retains its normal relative paths.  Changes below an ACP
    additional directory are rendered with their absolute root prefix, which
    makes their origin unambiguous in a tool card and avoids path collisions.
    """

    def __init__(
        self,
        observer: WorkspaceChangeObserver,
        additional_roots: Sequence[Path],
    ) -> None:
        self._observer = observer
        self._additional_roots = tuple(root.expanduser().resolve() for root in additional_roots)

    def capture(self, root: Path, /) -> WorkspaceChangeCheckpoint:
        roots = (root, *self._additional_roots)
        return _MultiRootWorkspaceChangeCheckpoint(
            tuple(self._observer.capture(candidate) for candidate in roots)
        )

    def compare(
        self,
        before: WorkspaceChangeCheckpoint,
        after: WorkspaceChangeCheckpoint,
        *,
        explicit_redactions: tuple[str, ...],
    ) -> WorkspaceChangeReport:
        previous = self._checkpoint(before)
        current = self._checkpoint(after)
        if len(previous.checkpoints) != len(current.checkpoints):
            raise TypeError("multi-root workspace checkpoints do not match")
        reports = tuple(
            self._observer.compare(
                earlier,
                later,
                explicit_redactions=explicit_redactions,
            )
            for earlier, later in zip(previous.checkpoints, current.checkpoints, strict=True)
        )
        changes: list[WorkspaceFileChange] = []
        omitted_files = 0
        for index, report in enumerate(reports):
            omitted_files += report.omitted_files
            root = self._additional_roots[index - 1] if index else None
            for change in report.files:
                if len(changes) >= _MAX_CHANGED_FILES:
                    omitted_files += 1
                    continue
                path = change.path if root is None else str(root / change.path)
                changes.append(
                    WorkspaceFileChange(
                        path=path,
                        status=change.status,
                        additions=change.additions,
                        deletions=change.deletions,
                        diff=change.diff,
                        diff_truncated=change.diff_truncated,
                        hidden_reason=change.hidden_reason,
                    )
                )
        return WorkspaceChangeReport(
            files=tuple(changes),
            omitted_files=omitted_files,
            scan_limited=any(report.scan_limited for report in reports),
        )

    @staticmethod
    def _checkpoint(checkpoint: WorkspaceChangeCheckpoint) -> _MultiRootWorkspaceChangeCheckpoint:
        if not isinstance(checkpoint, _MultiRootWorkspaceChangeCheckpoint):
            raise TypeError("workspace checkpoint belongs to a different observer")
        return checkpoint


def _workspace_change_report_from_comparison(
    comparison: Mapping[str, object],
) -> WorkspaceChangeReport:
    files_value = _required_value(comparison, "files")
    if not isinstance(files_value, list):
        raise TypeError("workspace comparison files must be a list")
    return WorkspaceChangeReport(
        files=tuple(_workspace_file_change_from_comparison(value) for value in files_value),
        omitted_files=_required_int(comparison, "omitted_files"),
        scan_limited=_required_bool(comparison, "scan_limited"),
    )


def _workspace_file_change_from_comparison(value: object) -> WorkspaceFileChange:
    comparison = _string_keyed_mapping(value)
    status = _workspace_change_status(_required_value(comparison, "status"))
    hidden_reason = _optional_hidden_reason(comparison, "hidden_reason")
    diff = _optional_string(comparison, "diff")
    diff_truncated = _optional_bool(comparison, "diff_truncated")
    if hidden_reason in ("sensitive", "large", "binary", "budget"):
        if diff is not None or diff_truncated is not None:
            raise TypeError("hidden workspace changes must not include diff details")
    elif diff is None or diff_truncated is None:
        raise TypeError("visible workspace changes must include diff details")
    return WorkspaceFileChange(
        path=_required_string(comparison, "path"),
        status=status,
        additions=_required_int(comparison, "additions"),
        deletions=_required_int(comparison, "deletions"),
        diff=diff,
        diff_truncated=diff_truncated,
        hidden_reason=hidden_reason,
    )


def _string_keyed_mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError("workspace comparison file detail must be a mapping")
    result: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise TypeError("workspace comparison mapping keys must be strings")
        result[key] = item
    return result


def _required_value(comparison: Mapping[str, object], name: str) -> object:
    try:
        return comparison[name]
    except KeyError as error:
        raise TypeError(f"workspace comparison is missing {name!r}") from error


def _required_string(comparison: Mapping[str, object], name: str) -> str:
    value = _required_value(comparison, name)
    if not isinstance(value, str):
        raise TypeError(f"workspace comparison {name!r} must be a string")
    return value


def _optional_string(comparison: Mapping[str, object], name: str) -> str | None:
    if name not in comparison:
        return None
    value = comparison[name]
    if not isinstance(value, str):
        raise TypeError(f"workspace comparison {name!r} must be a string")
    return value


def _required_int(comparison: Mapping[str, object], name: str) -> int:
    value = _required_value(comparison, name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"workspace comparison {name!r} must be an integer")
    return value


def _required_bool(comparison: Mapping[str, object], name: str) -> bool:
    value = _required_value(comparison, name)
    if not isinstance(value, bool):
        raise TypeError(f"workspace comparison {name!r} must be a boolean")
    return value


def _optional_bool(comparison: Mapping[str, object], name: str) -> bool | None:
    if name not in comparison:
        return None
    value = comparison[name]
    if not isinstance(value, bool):
        raise TypeError(f"workspace comparison {name!r} must be a boolean")
    return value


def _workspace_change_status(value: object) -> WorkspaceChangeStatus:
    if value == "created":
        return "created"
    if value == "deleted":
        return "deleted"
    if value == "modified":
        return "modified"
    raise TypeError("workspace comparison status is invalid")


def _optional_hidden_reason(
    comparison: Mapping[str, object],
    name: str,
) -> WorkspaceChangeHiddenReason | None:
    if name not in comparison:
        return None
    value = comparison[name]
    if value == "sensitive":
        return "sensitive"
    if value == "large":
        return "large"
    if value == "binary":
        return "binary"
    if value == "budget":
        return "budget"
    if value == "redacted":
        return "redacted"
    raise TypeError("workspace comparison hidden reason is invalid")


__all__ = [
    "FilesystemWorkspaceChangeObserver",
    "WorkspaceFileSnapshot",
    "WorkspaceSnapshot",
    "capture_workspace_snapshot",
    "compare_workspace_snapshots",
]
