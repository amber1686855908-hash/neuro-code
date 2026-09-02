from __future__ import annotations

import difflib
from collections.abc import Mapping, Sequence
from time import monotonic
from typing import Any

from rich.console import RenderableType
from rich.table import Table
from rich.text import Text

from neuro_code.interfaces.tui.controllers.base import TuiAppControllerMixin
from neuro_code.interfaces.tui.state import (
    _ERROR_MARK,
    _SUCCESS_MARK,
    _TOOL_EDIT_NAMES,
    _TOOL_READ_NAMES,
    _TOOL_SEARCH_NAMES,
    _TOOL_WAIT_NAMES,
    ToolActivityGroupState,
    ToolFeedbackState,
)
from neuro_code.interfaces.tui.text import ui_text
from neuro_code.interfaces.tui.theme import (
    ACCENT_WARNING,
    ERROR_TEXT_STYLE,
    TEXT_EMPHASIS,
    TOOL_ACTIVE_STYLE,
    TOOL_COMPLETE_STYLE,
    TOOL_DETAIL_STYLE,
    TOOL_GUIDE_STYLE,
    TOOL_META_STYLE,
    TOOL_TEXT_STYLE,
    TOOL_TITLE_STYLE,
)
from neuro_code.interfaces.tui.tool_activity import (
    TOOL_PEEK_LOGICAL_LINE_BUDGET,
    ToolActivityPeekPresentation,
    ToolCallSnapshot,
    ToolDisclosureLevel,
    ToolInspectorPresentation,
    ToolPeekLine,
    present_tool_activity_peek,
    present_tool_inspector,
)
from neuro_code.interfaces.tui.tool_activity.renderers import (
    bounded_inline,
    safe_tool_text,
)


class ToolActivityPresentationMixin(TuiAppControllerMixin):
    def _render_tool_activity_group(self, group: ToolActivityGroupState) -> RenderableType:
        title = ui_text(self._language, f"tool.activity.{self._tool_activity_kind(group)}")
        if group.disclosure is ToolDisclosureLevel.PEEK:
            return self._render_tool_activity_peek(group, title=title)

        table = Table.grid(expand=True, padding=(0, 1))
        table.add_column(width=1, no_wrap=True)
        table.add_column(ratio=1, overflow="ellipsis", no_wrap=True)
        table.add_column(width=8, justify="right", no_wrap=True)
        table.add_row("", Text(title, style=f"bold {TEXT_EMPHASIS}"), "")
        for marker, marker_style, summary, duration in self._tool_activity_rows(group):
            table.add_row(
                Text(marker, style=marker_style),
                Text(summary, style=TOOL_DETAIL_STYLE),
                Text(duration, style=TOOL_META_STYLE),
            )
        return table

    def _tool_activity_text(self, group: ToolActivityGroupState) -> str:
        """Stable Summary transcript independent from temporary UI disclosure."""

        title = ui_text(self._language, f"tool.activity.{self._tool_activity_kind(group)}")
        lines = [title]
        for marker, _, summary, duration in self._tool_activity_rows(group):
            suffix = f"  {duration}" if duration else ""
            lines.append(f"{marker} {summary}{suffix}")
        return "\n".join(lines)

    def _tool_call_snapshot(self, state: ToolFeedbackState) -> ToolCallSnapshot:
        return ToolCallSnapshot(
            call_id=state.call_id,
            name=state.name,
            arguments=dict(state.arguments),
            phase=state.phase,
            hosted=state.hosted,
            permission_effect=state.permission_effect,
            permission_reason=state.permission_reason,
            approval_effect=state.approval_effect,
            approval_outcome=state.approval_outcome,
            approval_reason=state.approval_reason,
            duration=state.duration,
            content=state.content,
            is_error=state.is_error,
            metadata=dict(state.metadata or {}),
            workspace_changes=self._tool_change_report(state),
            has_artifact=state.artifact_id is not None,
            artifact_content=state.artifact_content,
            artifact_stored_truncated=state.artifact_stored_truncated,
            artifact_read_truncated=state.artifact_read_truncated,
            artifact_loading=state.artifact_loading,
            artifact_unavailable=state.artifact_unavailable,
        )

    def _tool_activity_peek_presentation(
        self,
        group: ToolActivityGroupState,
        *,
        title: str,
    ) -> ToolActivityPeekPresentation:
        return present_tool_activity_peek(
            title=title,
            calls=tuple(self._tool_call_snapshot(state) for state in group.tools),
            selected_index=group.selected_tool_index,
            language=self._language,
            logical_line_budget=TOOL_PEEK_LOGICAL_LINE_BUDGET,
        )

    def _render_tool_activity_peek(
        self,
        group: ToolActivityGroupState,
        *,
        title: str,
    ) -> Text:
        peek = self._tool_activity_peek_presentation(group, title=title)
        rendered = Text(overflow="fold")
        rendered.append(peek.title, style=f"bold {TEXT_EMPHASIS}")
        rendered.append("\n")
        rendered.append(peek.help, style=TOOL_META_STYLE)
        rendered.append("\n")
        marker_style = (
            ERROR_TEXT_STYLE
            if peek.marker == _ERROR_MARK
            else TOOL_COMPLETE_STYLE
            if peek.marker == _SUCCESS_MARK
            else TOOL_ACTIVE_STYLE
        )
        rendered.append(f"{peek.marker} ", style=marker_style)
        rendered.append(f"{peek.position}  ", style=TOOL_META_STYLE)
        rendered.append(peek.selected_summary, style=TOOL_TITLE_STYLE)
        if peek.duration:
            rendered.append(f"  {peek.duration}", style=TOOL_META_STYLE)
        for line in peek.lines:
            rendered.append("\n  ", style=TOOL_GUIDE_STYLE)
            rendered.append(line.text, style=self._tool_peek_line_style(line))
        return rendered

    @staticmethod
    def _tool_peek_line_style(line: ToolPeekLine) -> str:
        if line.tone == "error":
            return ERROR_TEXT_STYLE
        if line.tone == "warning":
            return ACCENT_WARNING
        if line.tone == "primary":
            return TOOL_DETAIL_STYLE
        if line.tone == "output":
            return TOOL_TEXT_STYLE
        return TOOL_META_STYLE

    def _tool_inspector_presentation(
        self,
        state: ToolFeedbackState,
        group: ToolActivityGroupState,
    ) -> ToolInspectorPresentation:
        return present_tool_inspector(
            self._tool_call_snapshot(state),
            language=self._language,
            position=group.selected_tool_index + 1,
            total=len(group.tools),
        )

    def _tool_activity_kind(self, group: ToolActivityGroupState) -> str:
        names = {state.name for state in group.tools}
        if any(
            state.name in _TOOL_EDIT_NAMES or state.workspace_changes is not None
            for state in group.tools
        ):
            return "updating"
        if names and names <= _TOOL_WAIT_NAMES:
            return "waiting"
        if (
            names
            and names <= (_TOOL_READ_NAMES | _TOOL_SEARCH_NAMES | {"bash"})
            and (names & (_TOOL_READ_NAMES | _TOOL_SEARCH_NAMES))
        ):
            return "inspecting"
        return "working"

    def _tool_activity_rows(
        self,
        group: ToolActivityGroupState,
    ) -> tuple[tuple[str, str, str, str], ...]:
        if len(group.tools) == 1:
            state = group.tools[0]
            marker, marker_style = self._tool_status_marker((state,))
            summary = self._tool_summary_line(state)
            duration = self._tool_activity_duration((state,))
            return ((marker, marker_style, summary, duration),)

        buckets: dict[str, list[ToolFeedbackState]] = {}
        counts: dict[str, int] = {}
        for state in group.tools:
            bucket, count = self._tool_activity_bucket(state)
            buckets.setdefault(bucket, []).append(state)
            counts[bucket] = counts.get(bucket, 0) + count

        rows: list[tuple[str, str, str, str]] = []
        for bucket in ("read_files", "searched", "commands", "edits", "actions"):
            states = buckets.get(bucket)
            if not states:
                continue
            marker, marker_style = self._tool_status_marker(states)
            rows.append(
                (
                    marker,
                    marker_style,
                    self._tool_activity_count_label(bucket, counts[bucket]),
                    self._tool_activity_duration(states),
                )
            )
        for state in group.tools:
            if state.phase not in {"failed", "permission_denied", "approval_denied"}:
                continue
            reason = state.approval_reason or state.permission_reason or state.content
            rows.append(
                (
                    _ERROR_MARK,
                    ERROR_TEXT_STYLE,
                    f"{state.name} · {self._bounded_inline(reason, limit=96)}",
                    state.duration or "",
                )
            )
        return tuple(rows)

    @staticmethod
    def _tool_activity_bucket(state: ToolFeedbackState) -> tuple[str, int]:
        if state.name in _TOOL_READ_NAMES:
            if state.name == "read_files":
                raw_files = state.arguments.get("files")
                count = (
                    len(raw_files)
                    if isinstance(raw_files, Sequence) and not isinstance(raw_files, str | bytes)
                    else 1
                )
                return "read_files", max(1, count)
            return "read_files", 1
        if state.name in _TOOL_SEARCH_NAMES:
            return "searched", 1
        if state.name == "bash":
            return "commands", 1
        if state.name in _TOOL_EDIT_NAMES or state.workspace_changes is not None:
            return "edits", 1
        return "actions", 1

    def _tool_summary_line(self, state: ToolFeedbackState) -> str:
        display_name = {
            "read_file": "read",
            "read_files": "read",
        }.get(state.name, state.name)
        target = self._tool_summary_target(state)
        summary = f"{display_name}  {target}" if target else display_name
        if state.phase in {"failed", "permission_denied", "approval_denied"}:
            reason = state.approval_reason or state.permission_reason or state.content
            if reason:
                summary += f" · {self._bounded_inline(reason, limit=80)}"
        return summary

    def _tool_summary_target(self, state: ToolFeedbackState) -> str:
        if state.name == "bash":
            return self._bounded_inline(state.arguments.get("command"), limit=64)
        if state.name == "read_files":
            raw_files = state.arguments.get("files")
            count = (
                len(raw_files)
                if isinstance(raw_files, Sequence) and not isinstance(raw_files, str | bytes)
                else 0
            )
            return self._tool_activity_count_label("read_files", count)
        if state.name == "grep":
            query = self._bounded_inline(state.arguments.get("query"), limit=32)
            path = self._bounded_inline(state.arguments.get("path"), limit=28)
            return f"{query} · {path}"
        for key in ("path", "pattern", "query", "task_id", "name"):
            value = state.arguments.get(key)
            if isinstance(value, str) and value:
                return self._bounded_inline(value, limit=64)
        return ""

    def _tool_activity_count_label(self, bucket: str, count: int) -> str:
        suffix = ".one" if count == 1 else ""
        return ui_text(self._language, f"tool.activity.{bucket}{suffix}", count=count)

    def _tool_activity_duration(self, states: Sequence[ToolFeedbackState]) -> str:
        total = 0.0
        available = False
        now = monotonic()
        for state in states:
            if state.duration_seconds is not None:
                total += state.duration_seconds
                available = True
            elif state.started_at is not None:
                total += max(0.0, now - state.started_at)
                available = True
        return self._event_duration({"duration_seconds": total}) if available else ""

    @staticmethod
    def _tool_status_marker(states: Sequence[ToolFeedbackState]) -> tuple[str, str]:
        phases = {state.phase for state in states}
        if phases & {"failed", "permission_denied", "approval_denied"}:
            return _ERROR_MARK, ERROR_TEXT_STYLE
        if phases <= {"completed"}:
            return _SUCCESS_MARK, TOOL_COMPLETE_STYLE
        return "…", TOOL_ACTIVE_STYLE

    def _field(self, data: Mapping[str, Any], name: str) -> str:
        value = data.get(name)
        if isinstance(value, str) and value:
            return value
        return ui_text(self._language, "value.unknown")

    @staticmethod
    def _positive_int(value: object, *, fallback: int) -> int:
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
        return fallback

    @staticmethod
    def _bounded_inline(value: object, *, limit: int = 140) -> str:
        return bounded_inline(value, limit=limit)

    @staticmethod
    def _safe_tool_text(value: str) -> str:
        return safe_tool_text(value)

    @classmethod
    def _event_duration(cls, data: Mapping[str, Any]) -> str:
        seconds = cls._event_duration_seconds(data)
        if seconds is None:
            return "—"
        if seconds < 0.001:
            return "<1ms"
        if seconds < 1:
            return f"{seconds * 1000:.0f}ms"
        if seconds < 60:
            return f"{seconds:.1f}s"
        minutes, remainder = divmod(round(seconds), 60)
        return f"{minutes}m {remainder:02d}s"

    @staticmethod
    def _event_duration_seconds(data: Mapping[str, Any]) -> float | None:
        value = data.get("duration_seconds")
        if not isinstance(value, int | float) or isinstance(value, bool) or value < 0:
            return None
        return float(value)

    def _tool_change_report(self, state: ToolFeedbackState) -> dict[str, Any] | None:
        if state.workspace_changes is not None:
            raw_files = state.workspace_changes.get("files")
            if isinstance(raw_files, Sequence) and not isinstance(raw_files, str | bytes):
                return state.workspace_changes
        if state.phase != "completed":
            return None
        if state.name == "search_replace":
            path = state.arguments.get("path")
            old = state.arguments.get("old")
            new = state.arguments.get("new")
            if isinstance(path, str) and isinstance(old, str) and isinstance(new, str):
                diff_lines = list(
                    difflib.unified_diff(
                        old.splitlines(),
                        new.splitlines(),
                        fromfile=f"a/{path}",
                        tofile=f"b/{path}",
                        lineterm="",
                        n=3,
                    )
                )
                return {
                    "files": [
                        {
                            "path": path,
                            "status": "modified",
                            "additions": sum(
                                line.startswith("+") and not line.startswith("+++")
                                for line in diff_lines
                            ),
                            "deletions": sum(
                                line.startswith("-") and not line.startswith("---")
                                for line in diff_lines
                            ),
                            "diff": "\n".join(diff_lines),
                            "diff_truncated": False,
                        }
                    ],
                    "omitted_files": 0,
                    "scan_limited": False,
                }
        if state.name == "apply_patch":
            patch = next(
                (
                    value
                    for key in ("patch", "input")
                    if isinstance(value := state.arguments.get(key), str) and value
                ),
                None,
            )
            if patch is not None:
                path = state.arguments.get("path")
                display_path = path if isinstance(path, str) and path else "patch"
                return {
                    "files": [
                        {
                            "path": display_path,
                            "status": "modified",
                            "additions": sum(
                                line.startswith("+") and not line.startswith("+++")
                                for line in patch.splitlines()
                            ),
                            "deletions": sum(
                                line.startswith("-") and not line.startswith("---")
                                for line in patch.splitlines()
                            ),
                            "diff": patch,
                            "diff_truncated": False,
                        }
                    ],
                    "omitted_files": 0,
                    "scan_limited": False,
                }
        return None
