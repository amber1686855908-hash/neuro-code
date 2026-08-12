"""Pure presenters for Tool Activity peek and Inspector surfaces.

Tool Activity Peek 与 Inspector 界面的纯 presenter。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from neuro_code.interfaces.tui.tool_activity.models import (
    ToolActivityPeekPresentation,
    ToolCallSnapshot,
    ToolInspectorPresentation,
    ToolPeekLine,
)
from neuro_code.interfaces.tui.tool_activity.renderers import (
    bounded_inline,
    renderer_for,
    safe_tool_text,
)
from neuro_code.shared.ui_language import UiLanguage
from neuro_code.tui_text import ui_text

TOOL_PEEK_LOGICAL_LINE_BUDGET = 10
_INSPECTOR_INPUT_CHARACTER_BUDGET = 64 * 1024
_PRESENTATION_COLLECTION_LIMIT = 128
_PRESENTATION_DEPTH_LIMIT = 8
_SENSITIVE_KEY = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password|passwd|"
    r"authorization|private[_-]?key)"
)
_META_KEYS = (
    "exit_code",
    "truncated",
    "count",
    "files_matched",
    "scanned_files",
    "total_lines",
    "max_depth",
    "entry_limited",
    "result_limited",
    "scan_limited",
    "byte_limited",
    "names_only",
    "client_delegated",
    "output_artifact_bytes",
    "output_artifact_truncated",
)


def _status_marker(call: ToolCallSnapshot) -> str:
    if call.phase in {"failed", "permission_denied", "approval_denied"}:
        return "\N{MULTIPLICATION SIGN}"
    if call.phase == "completed":
        return "✓"
    return "…"


def _exceptional_permission_lines(
    call: ToolCallSnapshot,
    language: UiLanguage,
) -> tuple[ToolPeekLine, ...]:
    """Normal allow decisions are intentionally absent from Summary and Peek."""

    if call.permission_effect == "deny" or call.phase in {
        "permission_denied",
        "approval_denied",
    }:
        reason = call.approval_reason or call.permission_reason
        return (
            ToolPeekLine(
                ui_text(
                    language,
                    "tool.peek.permission_denied",
                    reason=bounded_inline(reason, limit=96),
                ),
                "error",
            ),
        )
    if call.permission_effect == "ask" and call.phase in {
        "approval_required",
        "awaiting_approval",
    }:
        return (ToolPeekLine(ui_text(language, "tool.peek.approval_required"), "warning"),)
    return ()


def present_tool_activity_peek(
    *,
    title: str,
    calls: Sequence[ToolCallSnapshot],
    selected_index: int,
    language: UiLanguage,
    logical_line_budget: int = TOOL_PEEK_LOGICAL_LINE_BUDGET,
) -> ToolActivityPeekPresentation:
    """Build one selected-call viewport within a strict logical-line budget."""

    if not calls:
        raise ValueError("tool activity peek requires at least one call")
    budget = max(4, logical_line_budget)
    selected = max(0, min(selected_index, len(calls) - 1))
    call = calls[selected]
    detail_budget = max(1, budget - 3)
    exceptional = _exceptional_permission_lines(call, language)
    renderer = renderer_for(call.name)
    rendered = renderer.render(
        call,
        language,
        budget=max(0, detail_budget - len(exceptional)),
    )
    lines = (*exceptional, *rendered.lines)[:detail_budget]
    target = rendered.target
    selected_summary = call.name if not target else f"{call.name}  {target}"
    return ToolActivityPeekPresentation(
        title=title,
        help=ui_text(language, "tool.peek.help"),
        position=ui_text(
            language,
            "tool.peek.position",
            current=selected + 1,
            total=len(calls),
        ),
        marker=_status_marker(call),
        selected_summary=selected_summary,
        duration=call.duration or "",
        lines=tuple(lines),
        logical_line_count=3 + len(lines),
    )


def _safe_value(value: Any, *, key: str = "", depth: int = 0) -> Any:
    if key and _SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if depth >= _PRESENTATION_DEPTH_LIMIT:
        return "[display depth limit]"
    if isinstance(value, Mapping):
        rendered: dict[str, Any] = {}
        items = list(value.items())
        for raw_key, item in items[:_PRESENTATION_COLLECTION_LIMIT]:
            item_key = safe_tool_text(str(raw_key))
            rendered[item_key] = _safe_value(item, key=item_key, depth=depth + 1)
        if len(items) > _PRESENTATION_COLLECTION_LIMIT:
            rendered["…"] = f"{len(items) - _PRESENTATION_COLLECTION_LIMIT} more items"
        return rendered
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        items = list(value)
        rendered_items = [
            _safe_value(item, depth=depth + 1) for item in items[:_PRESENTATION_COLLECTION_LIMIT]
        ]
        if len(items) > _PRESENTATION_COLLECTION_LIMIT:
            rendered_items.append(f"[{len(items) - _PRESENTATION_COLLECTION_LIMIT} more items]")
        return rendered_items
    if isinstance(value, str):
        return safe_tool_text(value)
    if value is None or isinstance(value, bool | int | float):
        return value
    return safe_tool_text(str(value))


def _input_document(call: ToolCallSnapshot, language: UiLanguage) -> str:
    rendered = json.dumps(
        _safe_value(call.arguments),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    rendered = safe_tool_text(rendered)
    if len(rendered) <= _INSPECTOR_INPUT_CHARACTER_BUDGET:
        return rendered
    marker = ui_text(language, "tool.inspector.input_truncated")
    return f"{rendered[:_INSPECTOR_INPUT_CHARACTER_BUDGET]}\n\n{marker}"


def _metadata_document(call: ToolCallSnapshot) -> str:
    rows: list[tuple[str, object]] = [
        ("tool", call.name),
        ("call_id", call.call_id),
        ("status", call.phase),
        ("duration", call.duration or "—"),
        ("hosted", call.hosted),
    ]
    if call.permission_effect is not None:
        permission = call.permission_effect
        if call.permission_reason:
            permission += f" · {bounded_inline(call.permission_reason, limit=240)}"
        rows.append(("permission", permission))
    if call.approval_outcome is not None:
        rows.append(("approval", call.approval_outcome))
    if call.is_error:
        rows.append(("error", True))
    for key in _META_KEYS:
        value = call.metadata.get(key)
        if value is not None:
            rows.append((key, _safe_value(value, key=key)))
    width = max(len(key) for key, _ in rows)
    return "\n".join(f"{key:<{width}}  {safe_tool_text(str(value))}" for key, value in rows)


def _workspace_changes_document(
    changes: Mapping[str, Any] | None,
    language: UiLanguage,
) -> str:
    if changes is None:
        return ""
    raw_files = changes.get("files")
    if not isinstance(raw_files, Sequence) or isinstance(raw_files, str | bytes):
        return ""
    sections: list[str] = []
    for raw_file in raw_files:
        if not isinstance(raw_file, Mapping):
            continue
        path = bounded_inline(raw_file.get("path"), limit=400)
        status = bounded_inline(raw_file.get("status"), limit=40)
        additions = raw_file.get("additions")
        deletions = raw_file.get("deletions")
        summary = f"{status}  {path}"
        if isinstance(additions, int) and isinstance(deletions, int):
            summary += f"  +{additions} -{deletions}"
        diff = raw_file.get("diff")
        sections.append(summary)
        if isinstance(diff, str) and diff:
            sections.append(safe_tool_text(diff))
        if raw_file.get("diff_truncated") is True:
            sections.append(ui_text(language, "tool.inspector.diff_truncated"))
        hidden_reason = raw_file.get("hidden_reason")
        if isinstance(hidden_reason, str) and hidden_reason:
            sections.append(
                ui_text(
                    language,
                    "tool.inspector.diff_unavailable",
                    reason=bounded_inline(hidden_reason, limit=80),
                )
            )
    omitted = changes.get("omitted_files")
    if isinstance(omitted, int) and omitted > 0:
        sections.append(ui_text(language, "tool.inspector.files_omitted", count=omitted))
    return "\n".join(sections)


def _output_document(call: ToolCallSnapshot, language: UiLanguage) -> tuple[str, str, bool]:
    artifact_loaded = call.artifact_content is not None
    content = call.artifact_content if call.artifact_content is not None else call.content or ""
    output = safe_tool_text(content)
    changes = _workspace_changes_document(call.workspace_changes, language)
    if changes:
        heading = ui_text(language, "tool.inspector.workspace_changes")
        output = f"{output}\n\n{heading}\n{changes}" if output else f"{heading}\n{changes}"
    if not output:
        output = ui_text(language, "tool.inspector.output_empty")

    stored_truncated = (
        call.artifact_stored_truncated or call.metadata.get("output_artifact_truncated") is True
    )
    inline_truncated = call.metadata.get("truncated") is True and not artifact_loaded
    read_truncated = call.artifact_read_truncated
    truncated = stored_truncated or inline_truncated or read_truncated
    notices: list[str] = []
    if call.artifact_loading:
        notices.append(ui_text(language, "tool.inspector.output_loading"))
    elif call.artifact_unavailable:
        notices.append(ui_text(language, "tool.inspector.output_unavailable"))
    elif call.has_artifact and not artifact_loaded:
        notices.append(ui_text(language, "tool.inspector.output_artifact_available"))
    if read_truncated:
        notices.append(ui_text(language, "tool.inspector.output_read_truncated"))
    if stored_truncated:
        notices.append(ui_text(language, "tool.inspector.output_stored_truncated"))
    elif inline_truncated:
        notices.append(ui_text(language, "tool.inspector.output_preview_truncated"))
    return output, " ".join(notices), truncated


def present_tool_inspector(
    call: ToolCallSnapshot,
    *,
    language: UiLanguage,
    position: int = 1,
    total: int = 1,
) -> ToolInspectorPresentation:
    output, notice, truncated = _output_document(call, language)
    position_text = ui_text(
        language,
        "tool.peek.position",
        current=position,
        total=total,
    )
    subtitle_parts = [call.name, position_text]
    if call.duration:
        subtitle_parts.append(call.duration)
    return ToolInspectorPresentation(
        title=ui_text(language, "tool.inspector.title"),
        subtitle="  ·  ".join(subtitle_parts),
        output=output,
        input=_input_document(call, language),
        meta=_metadata_document(call),
        output_notice=notice,
        output_truncated=truncated,
    )


__all__ = [
    "TOOL_PEEK_LOGICAL_LINE_BUDGET",
    "present_tool_activity_peek",
    "present_tool_inspector",
]
