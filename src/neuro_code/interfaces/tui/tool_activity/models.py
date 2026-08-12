"""Pure view models for progressive Tool Activity disclosure.

Tool Activity 渐进披露所使用的纯视图模型。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal


class ToolDisclosureLevel(StrEnum):
    """Conversation-local disclosure; full output never belongs to this state."""

    SUMMARY = "summary"
    PEEK = "peek"


class ToolInspectorTab(StrEnum):
    OUTPUT = "output"
    INPUT = "input"
    META = "meta"


ToolPeekTone = Literal["primary", "muted", "output", "warning", "error"]


@dataclass(frozen=True, slots=True)
class ToolPeekLine:
    text: str
    tone: ToolPeekTone = "muted"


@dataclass(frozen=True, slots=True)
class ToolPeekPresentation:
    """One renderer's bounded preview for a selected tool call."""

    renderer: str
    target: str
    lines: tuple[ToolPeekLine, ...] = ()


@dataclass(frozen=True, slots=True)
class ToolActivityPeekPresentation:
    """The complete logical-line budget for one Conversation peek viewport."""

    title: str
    help: str
    position: str
    marker: str
    selected_summary: str
    duration: str
    lines: tuple[ToolPeekLine, ...]
    logical_line_count: int


@dataclass(frozen=True, slots=True)
class ToolInspectorPresentation:
    """Selectable Inspector documents; none are rendered in Conversation."""

    title: str
    subtitle: str
    output: str
    input: str
    meta: str
    output_notice: str = ""
    output_truncated: bool = False

    def document(self, tab: ToolInspectorTab) -> str:
        if tab is ToolInspectorTab.INPUT:
            return self.input
        if tab is ToolInspectorTab.META:
            return self.meta
        return self.output


@dataclass(frozen=True, slots=True)
class ToolCallSnapshot:
    """TUI-owned immutable snapshot consumed by presentation code.

    It intentionally mirrors observable tool-event data without importing or
    changing domain/tool-execution models.
    """

    call_id: str
    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    phase: str = "requested"
    hosted: bool = False
    permission_effect: str | None = None
    permission_reason: str | None = None
    approval_effect: str | None = None
    approval_outcome: str | None = None
    approval_reason: str | None = None
    duration: str | None = None
    content: str | None = None
    is_error: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)
    workspace_changes: Mapping[str, Any] | None = None
    has_artifact: bool = False
    artifact_content: str | None = None
    artifact_stored_truncated: bool = False
    artifact_read_truncated: bool = False
    artifact_loading: bool = False
    artifact_unavailable: bool = False


__all__ = [
    "ToolActivityPeekPresentation",
    "ToolCallSnapshot",
    "ToolDisclosureLevel",
    "ToolInspectorPresentation",
    "ToolInspectorTab",
    "ToolPeekLine",
    "ToolPeekPresentation",
    "ToolPeekTone",
]
