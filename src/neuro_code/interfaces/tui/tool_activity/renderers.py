"""Metadata-first renderers for bounded Tool Activity peeks.

用于有界 Tool Activity 预览、优先消费 metadata 的 renderer。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Protocol

from neuro_code.interfaces.tui.tool_activity.models import (
    ToolCallSnapshot,
    ToolPeekLine,
    ToolPeekPresentation,
)
from neuro_code.shared.redaction import redact_sensitive_text
from neuro_code.shared.ui_language import UiLanguage
from neuro_code.tui_text import ui_text

_ANSI_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_LINE_CHARACTER_BUDGET = 180


def safe_tool_text(value: str) -> str:
    """Normalize terminal text and redact likely credentials for UI display."""

    normalized = _ANSI_ESCAPE.sub("", value.replace("\r\n", "\n").replace("\r", "\n"))
    printable = "".join(
        character if character in {"\n", "\t"} or ord(character) >= 32 else "�"
        for character in normalized
    )
    return redact_sensitive_text(printable)


def bounded_inline(value: object, *, limit: int = 96, empty: str = "—") -> str:
    if not isinstance(value, str) or not value:
        return empty
    rendered = " ".join(safe_tool_text(value).split())
    return rendered if len(rendered) <= limit else f"{rendered[: limit - 1]}…"


def _metadata_int(metadata: Mapping[str, object], key: str) -> int | None:
    value = metadata.get(key)
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _metadata_text(metadata: Mapping[str, object], key: str) -> str | None:
    value = metadata.get(key)
    return safe_tool_text(value) if isinstance(value, str) and value else None


def _target(call: ToolCallSnapshot, *keys: str, limit: int = 88) -> str:
    for key in keys:
        metadata_value = _metadata_text(call.metadata, key)
        if metadata_value:
            return bounded_inline(metadata_value, limit=limit)
        argument_value = call.arguments.get(key)
        if isinstance(argument_value, str) and argument_value:
            return bounded_inline(argument_value, limit=limit)
    return ""


def _fallback_lines(
    call: ToolCallSnapshot,
    language: UiLanguage,
    *,
    budget: int,
) -> tuple[ToolPeekLine, ...]:
    """Show raw result lines without interpreting their tool-specific format."""

    if budget <= 0:
        return ()
    content = safe_tool_text(call.content or "")
    raw_lines = content.splitlines()
    if not raw_lines:
        return (ToolPeekLine(ui_text(language, "tool.peek.output_empty"), "muted"),)

    lines: list[ToolPeekLine] = []
    if budget > 1:
        lines.append(ToolPeekLine(ui_text(language, "tool.peek.preview"), "muted"))
    visible_budget = max(1, budget - len(lines))
    reserve_omitted = len(raw_lines) > visible_budget and visible_budget > 1
    result_budget = visible_budget - int(reserve_omitted)
    for raw_line in raw_lines[:result_budget]:
        text = raw_line
        if len(text) > _LINE_CHARACTER_BUDGET:
            text = f"{text[: _LINE_CHARACTER_BUDGET - 1]}…"
        lines.append(ToolPeekLine(text, "output"))
    if reserve_omitted:
        lines.append(
            ToolPeekLine(
                ui_text(language, "tool.peek.lines_omitted", count=len(raw_lines) - result_budget),
                "muted",
            )
        )
    return tuple(lines[:budget])


def _truncation_line(call: ToolCallSnapshot, language: UiLanguage) -> ToolPeekLine | None:
    metadata = call.metadata
    truncated = any(
        metadata.get(key) is True
        for key in (
            "truncated",
            "output_artifact_truncated",
            "entry_limited",
            "result_limited",
            "scan_limited",
            "byte_limited",
        )
    )
    return (
        ToolPeekLine(ui_text(language, "tool.peek.result_truncated"), "warning")
        if truncated
        else None
    )


def _with_fallback(
    leading: Sequence[ToolPeekLine],
    call: ToolCallSnapshot,
    language: UiLanguage,
    *,
    budget: int,
) -> tuple[ToolPeekLine, ...]:
    lines = list(leading[:budget])
    truncation = _truncation_line(call, language)
    if truncation is not None and len(lines) < budget:
        lines.append(truncation)
    remaining = budget - len(lines)
    if remaining:
        lines.extend(_fallback_lines(call, language, budget=remaining))
    return tuple(lines[:budget])


class ToolPeekRenderer(Protocol):
    name: str

    def render(
        self,
        call: ToolCallSnapshot,
        language: UiLanguage,
        *,
        budget: int,
    ) -> ToolPeekPresentation: ...


class ListTreeRenderer:
    name = "list_tree"

    def render(
        self,
        call: ToolCallSnapshot,
        language: UiLanguage,
        *,
        budget: int,
    ) -> ToolPeekPresentation:
        target = _target(call, "path")
        count = _metadata_int(call.metadata, "count")
        depth = _metadata_int(call.metadata, "max_depth")
        leading: list[ToolPeekLine] = []
        if count is not None:
            leading.append(
                ToolPeekLine(
                    ui_text(
                        language,
                        "tool.peek.tree_summary",
                        count=count,
                        depth=depth if depth is not None else "—",
                    ),
                    "primary",
                )
            )
        return ToolPeekPresentation(
            self.name,
            target,
            _with_fallback(leading, call, language, budget=budget),
        )


class GrepRenderer:
    name = "grep"

    def render(
        self,
        call: ToolCallSnapshot,
        language: UiLanguage,
        *,
        budget: int,
    ) -> ToolPeekPresentation:
        query = _target(call, "query", limit=48)
        path = _target(call, "path", limit=48)
        target = " · ".join(value for value in (query, path) if value)
        count = _metadata_int(call.metadata, "count")
        files = _metadata_int(call.metadata, "files_matched")
        scanned = _metadata_int(call.metadata, "scanned_files")
        leading: list[ToolPeekLine] = []
        if count is not None:
            leading.append(
                ToolPeekLine(
                    ui_text(
                        language,
                        "tool.peek.grep_summary",
                        count=count,
                        files=files if files is not None else "—",
                    ),
                    "primary",
                )
            )
        if scanned is not None:
            leading.append(
                ToolPeekLine(
                    ui_text(language, "tool.peek.scanned_files", count=scanned),
                    "muted",
                )
            )
        # Current grep metadata exposes aggregate counts, not structured matches.
        # Preserve that boundary: formatted stdout remains a generic text fallback.
        return ToolPeekPresentation(
            self.name,
            target,
            _with_fallback(leading, call, language, budget=budget),
        )


class ReadFileRenderer:
    name = "read_file"

    def render(
        self,
        call: ToolCallSnapshot,
        language: UiLanguage,
        *,
        budget: int,
    ) -> ToolPeekPresentation:
        target = _target(call, "path")
        total_lines = _metadata_int(call.metadata, "total_lines")
        start = call.arguments.get("start_line", 1)
        maximum = call.arguments.get("max_lines")
        leading: list[ToolPeekLine] = []
        if total_lines is not None:
            leading.append(
                ToolPeekLine(
                    ui_text(language, "tool.peek.file_summary", count=total_lines),
                    "primary",
                )
            )
        if isinstance(start, int) and not isinstance(start, bool) and isinstance(maximum, int):
            leading.append(
                ToolPeekLine(
                    ui_text(
                        language,
                        "tool.peek.requested_range",
                        start=start,
                        end=start + max(0, maximum - 1),
                    ),
                    "muted",
                )
            )
        return ToolPeekPresentation(
            self.name,
            target,
            _with_fallback(leading, call, language, budget=budget),
        )


class BashRenderer:
    name = "bash"

    def render(
        self,
        call: ToolCallSnapshot,
        language: UiLanguage,
        *,
        budget: int,
    ) -> ToolPeekPresentation:
        target = _target(call, "command", limit=104)
        exit_code = call.metadata.get("exit_code")
        leading: list[ToolPeekLine] = []
        if isinstance(exit_code, int) and not isinstance(exit_code, bool):
            leading.append(
                ToolPeekLine(
                    ui_text(language, "tool.peek.exit_code", code=exit_code),
                    "error" if exit_code else "primary",
                )
            )
        return ToolPeekPresentation(
            self.name,
            target,
            _with_fallback(leading, call, language, budget=budget),
        )


class GenericToolRenderer:
    name = "generic"

    def render(
        self,
        call: ToolCallSnapshot,
        language: UiLanguage,
        *,
        budget: int,
    ) -> ToolPeekPresentation:
        target = _target(call, "path", "query", "pattern", "task_id", "name", "command")
        count = _metadata_int(call.metadata, "count")
        leading = (
            [
                ToolPeekLine(
                    ui_text(language, "tool.peek.result_count", count=count),
                    "primary",
                )
            ]
            if count is not None
            else []
        )
        return ToolPeekPresentation(
            self.name,
            target,
            _with_fallback(leading, call, language, budget=budget),
        )


_LIST_TREE_RENDERER = ListTreeRenderer()
_GREP_RENDERER = GrepRenderer()
_READ_FILE_RENDERER = ReadFileRenderer()
_BASH_RENDERER = BashRenderer()
_GENERIC_RENDERER = GenericToolRenderer()


def renderer_for(name: str) -> ToolPeekRenderer:
    if name == "list_tree":
        return _LIST_TREE_RENDERER
    if name in {"grep", "grep_many"}:
        return _GREP_RENDERER
    if name in {"read_file", "read_files"}:
        return _READ_FILE_RENDERER
    if name == "bash":
        return _BASH_RENDERER
    return _GENERIC_RENDERER


__all__ = [
    "BashRenderer",
    "GenericToolRenderer",
    "GrepRenderer",
    "ListTreeRenderer",
    "ReadFileRenderer",
    "ToolPeekRenderer",
    "bounded_inline",
    "renderer_for",
    "safe_tool_text",
]
