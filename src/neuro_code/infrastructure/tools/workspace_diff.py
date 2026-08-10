"""Task-scoped workspace change journal and bounded diff tool.

The journal records the first observed state before a structured mutation and
keeps the current state in memory for the active agent task.  It deliberately
does not use Git: pre-existing user changes remain outside the task baseline.

任务级工作区变更日志和有界差异工具.

日志在结构化修改前记录首次观察到的状态,并在当前 Agent 任务内保留最新状态.
它有意不依赖 Git,因此用户在任务开始前已有的修改不会被错误归因.
"""

from __future__ import annotations

import difflib
import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from neuro_code.application.ports.tools import ToolContext
from neuro_code.application.ports.workspace_changes import (
    WorkspaceChangeHiddenReason,
    WorkspaceChangeReport,
    WorkspaceChangeStatus,
    WorkspaceDiffFile,
    WorkspaceDiffMove,
    WorkspaceDiffResult,
)
from neuro_code.domain.tools import ToolDefinition, ToolResult
from neuro_code.infrastructure.workspace.changes import (
    WorkspaceFileSnapshot,
    WorkspaceSnapshot,
    capture_workspace_paths,
    capture_workspace_snapshot,
    workspace_change_report_from_snapshots,
)
from neuro_code.infrastructure.workspace.paths import (
    resolve_workspace_path,
    workspace_display_path,
)
from neuro_code.shared.async_utils import run_blocking
from neuro_code.shared.errors import ToolError
from neuro_code.shared.redaction import redact_sensitive_text

MAX_WORKSPACE_DIFF_FILES = 200
MAX_WORKSPACE_DIFF_BYTES = 200_000
MAX_WORKSPACE_DIFF_CONTEXT_LINES = 20
MAX_WORKSPACE_DIFF_PATHS = 100
_STRUCTURED_EDIT_TOOLS = frozenset({"apply_patch", "search_replace"})


@dataclass(slots=True)
class _SnapshotBundle:
    files: dict[str, WorkspaceFileSnapshot]
    scan_limited: bool
    targeted: bool = False


@dataclass(slots=True)
class _JournalEntry:
    baseline: WorkspaceFileSnapshot | None
    current: WorkspaceFileSnapshot | None


def _snapshot_changed(
    before: WorkspaceFileSnapshot | None,
    after: WorkspaceFileSnapshot | None,
) -> bool:
    if before is None or after is None:
        return before is not after
    if before.digest is not None and after.digest is not None:
        return before.digest != after.digest
    return (
        before.size,
        before.modified_ns,
        before.hidden_reason,
    ) != (
        after.size,
        after.modified_ns,
        after.hidden_reason,
    )


def _capture_bundle(
    roots: tuple[Path, ...],
    *,
    explicit_redactions: tuple[str, ...],
    target_paths: tuple[str, ...] = (),
) -> _SnapshotBundle:
    files: dict[str, WorkspaceFileSnapshot] = {}
    scan_limited = False
    for index, root in enumerate(roots):
        resolved_root = root.expanduser().resolve()
        root_targets: tuple[str, ...]
        if target_paths:
            selected: list[str] = []
            for raw_path in target_paths:
                candidate = Path(raw_path).expanduser()
                if index == 0 and not candidate.is_absolute():
                    selected.append(candidate.as_posix())
                    continue
                if candidate.is_absolute():
                    try:
                        selected.append(
                            candidate.resolve(strict=False).relative_to(resolved_root).as_posix()
                        )
                    except ValueError:
                        continue
            root_targets = tuple(selected)
            snapshot = capture_workspace_paths(resolved_root, root_targets)
        else:
            root_targets = ()
            snapshot = capture_workspace_snapshot(resolved_root)
        scan_limited = scan_limited or snapshot.scan_limited
        for relative, value in snapshot.files.items():
            key = relative if index == 0 else (resolved_root / relative).as_posix()
            text = value.text
            hidden_reason = value.hidden_reason
            if text is not None:
                safe_text = redact_sensitive_text(text, explicit_values=explicit_redactions)
                if safe_text != text and hidden_reason is None:
                    hidden_reason = "redacted"
                text = safe_text
            files[key] = WorkspaceFileSnapshot(
                value.size,
                value.modified_ns,
                value.digest,
                text,
                hidden_reason,
            )
    return _SnapshotBundle(files, scan_limited, bool(target_paths))


def _same_key_for_path(raw_path: str, roots: tuple[Path, ...]) -> str:
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        return candidate.as_posix().removeprefix("./")
    resolved = candidate.resolve(strict=False)
    primary = roots[0].expanduser().resolve()
    try:
        return resolved.relative_to(primary).as_posix()
    except ValueError:
        return resolved.as_posix()


def _path_matches(key: str, filters: tuple[str, ...]) -> bool:
    if not filters:
        return True
    for raw_filter in filters:
        normalized = raw_filter.rstrip("/") or "."
        if normalized == ".":
            return True
        if key == normalized or key.startswith(f"{normalized}/"):
            return True
    return False


def _bounded_text(text: str, max_bytes: int) -> tuple[str, bool]:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, False
    if max_bytes <= 0:
        return "", True
    clipped = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return clipped, True


def _render_diff(
    before: WorkspaceFileSnapshot | None,
    after: WorkspaceFileSnapshot | None,
    *,
    path: str,
    status: WorkspaceChangeStatus,
    context_lines: int,
) -> tuple[str, int, int, bool, bool]:
    before_text = before.text if before is not None and before.text is not None else ""
    after_text = after.text if after is not None and after.text is not None else ""
    before_text = redact_sensitive_text(before_text)
    after_text = redact_sensitive_text(after_text)
    redacted = (before is not None and before.text is not None and before.text != before_text) or (
        after is not None and after.text is not None and after.text != after_text
    )
    lines = list(
        difflib.unified_diff(
            before_text.splitlines(),
            after_text.splitlines(),
            fromfile="/dev/null" if status == "created" else f"a/{path}",
            tofile="/dev/null" if status == "deleted" else f"b/{path}",
            n=context_lines,
            lineterm="",
        )
    )
    additions = sum(line.startswith("+") and not line.startswith("+++") for line in lines)
    deletions = sum(line.startswith("-") and not line.startswith("---") for line in lines)
    return "\n".join(lines), additions, deletions, False, redacted


class WorkspaceMutationJournal:
    """Keep first-write baselines for one in-memory agent task.

    为一个内存中的 Agent 任务保留首次写入前的基线.

    ``begin_task`` is intentionally the lifecycle boundary, while a tool
    invocation is only an execution segment inside that task.  The current
    runtime has no suspended/resumed task yet, so ``AgentLoopRunner.run`` is
    the complete user-task boundary and calls ``begin_task`` once per turn.
    A future suspend/resume coordinator must keep the same journal and call
    ``begin_task`` only when a new logical task is created.

    ``begin_task`` 是生命周期边界,工具调用只是任务内的执行段. 当前运行时还没有
    挂起/恢复任务,因此 ``AgentLoopRunner.run`` 就是完整用户任务边界,每回合调用
    一次 ``begin_task``. 未来的挂起/恢复协调器必须复用同一个日志,只在创建新的
    逻辑任务时调用 ``begin_task``.
    """

    def __init__(self) -> None:
        self._entries: dict[str, _JournalEntry] = {}
        self._moves: list[WorkspaceDiffMove] = []
        self._last_snapshot: _SnapshotBundle | None = None
        self._pending_snapshot: _SnapshotBundle | None = None
        self._last_change_report: WorkspaceChangeReport | None = None
        self._scan_limited = False
        self._structured_edits = False
        self._workspace_observed = False
        self._partial = False
        self._unattributed_changes = False
        self._lock = threading.RLock()

    def begin_task(self) -> None:
        with self._lock:
            self._entries.clear()
            self._moves.clear()
            self._last_snapshot = None
            self._pending_snapshot = None
            self._last_change_report = None
            self._scan_limited = False
            self._structured_edits = False
            self._workspace_observed = False
            self._partial = False
            self._unattributed_changes = False

    def before_mutation(
        self,
        roots: tuple[Path, ...],
        *,
        tool_name: str,
        explicit_redactions: tuple[str, ...],
        target_paths: tuple[str, ...] = (),
    ) -> None:
        snapshot = _capture_bundle(
            roots,
            explicit_redactions=explicit_redactions,
            target_paths=target_paths,
        )
        with self._lock:
            if not snapshot.targeted and self._last_snapshot is not None:
                self._mark_unattributed(self._last_snapshot, snapshot)
            self._pending_snapshot = snapshot
            self._scan_limited = self._scan_limited or snapshot.scan_limited

    def after_mutation(
        self,
        roots: tuple[Path, ...],
        *,
        tool_name: str,
        mutation_metadata: Mapping[str, object] | None,
        explicit_redactions: tuple[str, ...],
        target_paths: tuple[str, ...] = (),
    ) -> None:
        after = _capture_bundle(
            roots,
            explicit_redactions=explicit_redactions,
            target_paths=target_paths,
        )
        with self._lock:
            before = self._pending_snapshot
            self._pending_snapshot = None
            self._scan_limited = self._scan_limited or after.scan_limited
            self._workspace_observed = True
            if before is None:
                self._partial = True
                if not after.targeted:
                    self._last_snapshot = after
                return
            self._scan_limited = self._scan_limited or before.scan_limited
            changed = False
            for key in sorted(before.files.keys() | after.files.keys()):
                old = before.files.get(key)
                new = after.files.get(key)
                if not _snapshot_changed(old, new):
                    continue
                changed = True
                entry = self._entries.get(key)
                if entry is None:
                    self._entries[key] = _JournalEntry(old, new)
                else:
                    entry.current = new
            if changed and tool_name in _STRUCTURED_EDIT_TOOLS:
                self._structured_edits = True
            if tool_name not in _STRUCTURED_EDIT_TOOLS:
                # Bash, background tasks, and client-side processes can mutate
                # files outside this exact observation window.
                self._partial = self._partial or changed
            if self._scan_limited:
                self._partial = True
            if mutation_metadata is not None:
                self._record_moves(mutation_metadata, roots)
            if after.targeted:
                self._last_change_report = workspace_change_report_from_snapshots(
                    WorkspaceSnapshot(before.files, before.scan_limited),
                    WorkspaceSnapshot(after.files, after.scan_limited),
                    explicit_redactions=explicit_redactions,
                )
            else:
                self._last_snapshot = after

    def last_change_report(
        self,
        *,
        explicit_redactions: tuple[str, ...],
    ) -> WorkspaceChangeReport | None:
        del explicit_redactions
        with self._lock:
            report = self._last_change_report
            return report if report is not None and report.should_emit else None

    def record_external_observation(self, report: WorkspaceChangeReport) -> None:
        """Record observer evidence without taking a second journal snapshot."""

        with self._lock:
            self._workspace_observed = True
            if report.files or report.scan_limited:
                self._partial = True
            self._scan_limited = self._scan_limited or report.scan_limited

    def diff(
        self,
        roots: tuple[Path, ...],
        *,
        paths: tuple[str, ...],
        max_files: int,
        max_diff_bytes: int,
        context_lines: int,
        explicit_redactions: tuple[str, ...],
    ) -> WorkspaceDiffResult:
        current = _capture_bundle(roots, explicit_redactions=explicit_redactions)
        with self._lock:
            if self._last_snapshot is not None:
                self._mark_unattributed(self._last_snapshot, current)
            self._last_snapshot = current
            self._scan_limited = self._scan_limited or current.scan_limited
            for key, entry in self._entries.items():
                entry.current = current.files.get(key)
            candidates = sorted(
                key
                for key, entry in self._entries.items()
                if _snapshot_changed(entry.baseline, entry.current) and _path_matches(key, paths)
            )
            omitted_files = max(0, len(candidates) - max_files)
            files: list[WorkspaceDiffFile] = []
            remaining = max_diff_bytes
            truncated = self._scan_limited
            for key in candidates[:max_files]:
                entry = self._entries[key]
                status = self._status(entry.baseline, entry.current)
                hidden_reason = self._hidden_reason(entry.baseline, entry.current)
                if hidden_reason is not None:
                    files.append(
                        WorkspaceDiffFile(
                            key,
                            status,
                            0,
                            0,
                            None,
                            False,
                            hidden_reason,
                        )
                    )
                    continue
                diff, additions, deletions, _ignored, _redacted = _render_diff(
                    entry.baseline,
                    entry.current,
                    path=key,
                    status=status,
                    context_lines=context_lines,
                )
                bounded, was_truncated = _bounded_text(diff, remaining)
                if was_truncated:
                    truncated = True
                if not bounded and diff:
                    files.append(
                        WorkspaceDiffFile(
                            key,
                            status,
                            additions,
                            deletions,
                            None,
                            True,
                            "budget",
                        )
                    )
                else:
                    files.append(
                        WorkspaceDiffFile(
                            key,
                            status,
                            additions,
                            deletions,
                            bounded,
                            was_truncated,
                            None,
                        )
                    )
                remaining = max(0, remaining - len(bounded.encode("utf-8")))
            if omitted_files:
                truncated = True
            moves = tuple(
                move
                for move in self._moves
                if _path_matches(move.old_path, paths) or _path_matches(move.new_path, paths)
            )
            return WorkspaceDiffResult(
                files=tuple(files),
                moved_files=moves,
                omitted_files=omitted_files,
                scan_limited=self._scan_limited,
                truncated=truncated,
                structured_edits=self._structured_edits,
                workspace_observed=self._workspace_observed,
                partial=self._partial,
                unattributed_changes_detected=self._unattributed_changes,
            )

    def _mark_unattributed(self, before: _SnapshotBundle, after: _SnapshotBundle) -> None:
        for key in before.files.keys() | after.files.keys():
            if key in self._entries:
                continue
            if _snapshot_changed(before.files.get(key), after.files.get(key)):
                self._unattributed_changes = True
                self._partial = True

    def _record_moves(self, metadata: Mapping[str, object], roots: tuple[Path, ...]) -> None:
        raw_moves = metadata.get("moved_files")
        if not isinstance(raw_moves, list):
            return
        for raw_move in raw_moves:
            if not isinstance(raw_move, Mapping):
                continue
            old_raw = raw_move.get("from")
            new_raw = raw_move.get("to")
            if not isinstance(old_raw, str) or not isinstance(new_raw, str):
                continue
            old_path = _same_key_for_path(old_raw, roots)
            new_path = _same_key_for_path(new_raw, roots)
            if old_path == new_path:
                continue
            move = WorkspaceDiffMove(old_path, new_path)
            if move not in self._moves:
                self._moves.append(move)

    @staticmethod
    def _status(
        before: WorkspaceFileSnapshot | None,
        after: WorkspaceFileSnapshot | None,
    ) -> WorkspaceChangeStatus:
        if before is None:
            return "created"
        if after is None:
            return "deleted"
        return "modified"

    @staticmethod
    def _hidden_reason(
        before: WorkspaceFileSnapshot | None,
        after: WorkspaceFileSnapshot | None,
    ) -> WorkspaceChangeHiddenReason | None:
        for snapshot in (before, after):
            reason = snapshot.hidden_reason if snapshot is not None else None
            if reason in {
                "sensitive",
                "large",
                "binary",
                "budget",
                "redacted",
            }:
                return cast(WorkspaceChangeHiddenReason, reason)
        return None


def _ensure_no_link_components(context: ToolContext, raw_path: str) -> None:
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = context.cwd / candidate
    # Ignore symlink aliases above the workspace root (for example macOS's
    # /var -> /private/var).  Security checks apply to components inside an
    # explicitly allowed root, not to the root spelling itself.
    roots = (context.cwd, *context.additional_workspace_roots)
    for root in roots:
        root_path = root.expanduser()
        try:
            relative = candidate.relative_to(root_path)
        except ValueError:
            continue
        current = root_path
        relative_parts = relative.parts
        for part in relative_parts[:-1]:
            current /= part
            if _is_link_like(current):
                raise ToolError("workspace diff paths must not traverse symlinks or junctions")
        if _is_link_like(candidate):
            raise ToolError("workspace diff paths must not target symlinks or junctions")
        return


def _is_link_like(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        return bool(is_junction is not None and is_junction())
    except OSError:
        return True


def _protected_redactions(context: ToolContext) -> tuple[str, ...]:
    protected = {name.casefold() for name in context.protected_environment_variables}
    return tuple(
        dict.fromkeys(
            value for name, value in os.environ.items() if name.casefold() in protected and value
        )
    )


class WorkspaceDiffTool:
    """Show bounded changes attributed to the current task journal."""

    definition = ToolDefinition(
        name="workspace_diff",
        description=(
            "Review bounded workspace changes made during the current agent task after "
            "workspace edits, before verification or the final response. Do not assume "
            "this represents Git HEAD differences or unrelated pre-existing user changes."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": MAX_WORKSPACE_DIFF_PATHS,
                },
                "max_files": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_WORKSPACE_DIFF_FILES,
                    "default": 50,
                },
                "max_diff_bytes": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_WORKSPACE_DIFF_BYTES,
                    "default": 50_000,
                },
                "context_lines": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": MAX_WORKSPACE_DIFF_CONTEXT_LINES,
                    "default": 3,
                },
            },
            "additionalProperties": False,
        },
    )
    side_effecting = False

    async def execute(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        if context.client_file_system is not None:
            raise ToolError("workspace_diff requires local workspace observation")
        journal = context.workspace_change_journal
        if journal is None:
            raise ToolError("workspace diff is unavailable for this runtime")
        paths = arguments.get("paths", ())
        if not isinstance(paths, (list, tuple)) or not all(isinstance(path, str) for path in paths):
            raise ToolError("paths must be a list of strings")
        if len(paths) > MAX_WORKSPACE_DIFF_PATHS:
            raise ToolError(f"paths must contain at most {MAX_WORKSPACE_DIFF_PATHS} items")
        normalized_paths: list[str] = []
        for raw_path in paths:
            _ensure_no_link_components(context, raw_path)
            resolved = resolve_workspace_path(
                context.cwd,
                raw_path,
                must_exist=False,
                additional_workspace_roots=context.additional_workspace_roots,
            )
            normalized_paths.append(
                workspace_display_path(
                    context.cwd,
                    resolved,
                    context.additional_workspace_roots,
                )
            )
        max_files = self._bounded_int(
            arguments.get("max_files", 50), "max_files", 1, MAX_WORKSPACE_DIFF_FILES
        )
        max_diff_bytes = self._bounded_int(
            arguments.get("max_diff_bytes", 50_000),
            "max_diff_bytes",
            1,
            MAX_WORKSPACE_DIFF_BYTES,
        )
        context_lines = self._bounded_int(
            arguments.get("context_lines", 3),
            "context_lines",
            0,
            MAX_WORKSPACE_DIFF_CONTEXT_LINES,
        )
        roots = (context.cwd, *context.additional_workspace_roots)
        result = await run_blocking(
            journal.diff,
            roots,
            paths=tuple(normalized_paths),
            max_files=max_files,
            max_diff_bytes=max_diff_bytes,
            context_lines=context_lines,
            explicit_redactions=tuple(
                dict.fromkeys((*context.redaction_values, *_protected_redactions(context)))
            ),
        )
        content_parts: list[str] = []
        for move in result.moved_files:
            content_parts.append(f"renamed: {move.old_path} -> {move.new_path}")
        for file in result.files:
            if file.diff is not None:
                content_parts.append(file.diff)
            else:
                reason = file.hidden_reason or "unavailable"
                content_parts.append(f"{file.status}: {file.path} [{reason}]")
        if not content_parts:
            content_parts.append("[no workspace changes recorded for this task]")
        if result.truncated:
            content_parts.append("[diff truncated]")
        content, content_truncated = self._bounded_output(
            "\n\n".join(content_parts), context.output_byte_limit
        )
        metadata = {
            "changed_files": [file.path for file in result.files],
            "modified_files": [file.path for file in result.files if file.status == "modified"],
            "added_files": [file.path for file in result.files if file.status == "created"],
            "deleted_files": [file.path for file in result.files if file.status == "deleted"],
            "moved_files": [
                {"from": move.old_path, "to": move.new_path} for move in result.moved_files
            ],
            "file_count": len(result.files),
            "addition_count": sum(file.additions for file in result.files),
            "deletion_count": sum(file.deletions for file in result.files),
            "omitted_files": result.omitted_files,
            "truncated": result.truncated or content_truncated,
            "coverage": {
                "structured_edits": result.structured_edits,
                "workspace_observed": result.workspace_observed,
                "partial": result.partial,
            },
            "unattributed_changes_detected": result.unattributed_changes_detected,
        }
        return ToolResult(content, metadata=metadata)

    @staticmethod
    def _bounded_int(value: object, name: str, minimum: int, maximum: int) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
            raise ToolError(f"{name} must be an integer between {minimum} and {maximum}")
        return value

    @staticmethod
    def _bounded_output(content: str, limit: int) -> tuple[str, bool]:
        encoded = content.encode("utf-8")
        if len(encoded) <= limit:
            return content, False
        clipped = encoded[:limit].decode("utf-8", errors="ignore")
        return f"{clipped}\n[diff truncated]", True


__all__ = ["WorkspaceDiffTool", "WorkspaceMutationJournal"]
