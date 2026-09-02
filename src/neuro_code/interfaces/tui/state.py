"""TUI-local state models and bounded runtime constants.

TUI 本地状态模型与有界运行时常量.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from textual.worker import Worker

from neuro_code.domain.background_tasks.models import BackgroundWakeLimits
from neuro_code.interfaces.tui.tool_activity import ToolDisclosureLevel, ToolInspectorScreen

_RESTORED_MESSAGE_LIMIT = 20_000
_TASK_LIST_LIMIT = 20
_TASK_POLL_SECONDS = 0.5
_MAX_QUEUED_INTERJECTIONS = 4
_DEFAULT_BACKGROUND_WAKE_LIMITS = BackgroundWakeLimits()
_TERMINAL_SIZE_POLL_SECONDS = 0.25
_LOADING_ANIMATION_TICK_SECONDS = 0.05
_TOOL_ELAPSED_UPDATE_SECONDS = 0.25
_PROMPT_MAX_VISIBLE_LINES = 8
_COMMAND_HINT_LIMIT = 5
_TOOL_READ_NAMES = frozenset({"read_file", "read_files", "view_image"})
_TOOL_SEARCH_NAMES = frozenset({"glob", "grep", "grep_many", "list_dir", "list_tree", "skill"})
_TOOL_EDIT_NAMES = frozenset({"apply_patch", "search_replace", "write_file"})
_TOOL_WAIT_NAMES = frozenset({"task_output", "wait_for_tasks", "wait_tasks"})
TUI_RELOAD_PROVIDER_SETTINGS = 75
_PROMPT_MARK = "\N{SINGLE RIGHT-POINTING ANGLE QUOTATION MARK}"
_ERROR_MARK = "\N{MULTIPLICATION SIGN}"
_SUCCESS_MARK = "\N{CHECK MARK}"
_WARNING_MARK = "!"


@dataclass(slots=True)
class CollapsingPulseAnimation:
    """Textual-friendly port of the user-supplied collapsing pulse demo.

    提供适配 Textual 的用户折叠脉冲动画示例."""

    width: int = 7
    level_by_distance: tuple[int, ...] = (7, 5, 3, 1)
    peak_position: int = 0
    direction: int = 1
    merged_trail_count: int = 0
    phase: str = "moving"

    @property
    def delay_seconds(self) -> float:
        return {
            "moving": 0.09,
            "edge-hold": 0.14,
            "collapsing": 0.10,
            "merged-hold": 0.22,
        }.get(self.phase, 0.09)

    def reset(self) -> None:
        self.peak_position = 0
        self.direction = 1
        self.merged_trail_count = 0
        self.phase = "moving"

    def advance(self) -> None:
        trail_length = max(0, len(self.level_by_distance) - 1)
        if self.phase == "moving":
            self.peak_position += self.direction
            self.merged_trail_count = 0
            reached_edge = (self.direction == 1 and self.peak_position >= self.width - 1) or (
                self.direction == -1 and self.peak_position <= 0
            )
            if reached_edge:
                self.peak_position = max(0, min(self.width - 1, self.peak_position))
                self.phase = "edge-hold"
            return
        if self.phase == "edge-hold":
            if trail_length == 0:
                self.phase = "merged-hold"
                return
            self.merged_trail_count = 1
            self.phase = "merged-hold" if self.merged_trail_count >= trail_length else "collapsing"
            return
        if self.phase == "collapsing":
            self.merged_trail_count = min(
                trail_length,
                self.merged_trail_count + 1,
            )
            if self.merged_trail_count >= trail_length:
                self.phase = "merged-hold"
            return
        if self.phase == "merged-hold":
            self.direction *= -1
            self.peak_position += self.direction
            self.merged_trail_count = 0
            self.phase = "moving"
            return
        self.reset()

    def levels(self) -> tuple[int, ...]:
        trail = self.level_by_distance[1 + self.merged_trail_count :]
        rendered: list[int] = []
        for position in range(self.width):
            if position == self.peak_position:
                rendered.append(self.level_by_distance[0])
                continue
            if self.direction == 1:
                distance = self.peak_position - position
            else:
                distance = position - self.peak_position
            trail_index = distance - 1
            rendered.append(trail[trail_index] if 0 <= trail_index < len(trail) else 0)
        return tuple(rendered)


@dataclass(frozen=True, slots=True)
class TranscriptEntry:
    category: str
    text: str
    ui_key: str | None = None
    ui_values: tuple[tuple[str, object], ...] = ()


@dataclass(slots=True)
class ToolFeedbackState:
    call_id: str
    name: str
    arguments: dict[str, Any]
    entry_index: int
    hosted: bool = False
    phase: str = "requested"
    permission_effect: str | None = None
    permission_reason: str | None = None
    approval_effect: str | None = None
    approval_outcome: str | None = None
    approval_reason: str | None = None
    duration: str | None = None
    duration_seconds: float | None = None
    started_at: float | None = None
    content: str | None = None
    is_error: bool = False
    metadata: dict[str, Any] | None = None
    workspace_changes: dict[str, Any] | None = None
    artifact_id: str | None = None
    artifact_content: str | None = None
    artifact_stored_truncated: bool = False
    artifact_read_truncated: bool = False
    artifact_loading: bool = False
    artifact_unavailable: bool = False


@dataclass(slots=True)
class ToolActivityGroupState:
    """Consecutive tool calls projected as one presentation-only activity block.

    连续工具调用在表现层聚合为一个活动块,不改变领域事件或执行语义。
    """

    tools: list[ToolFeedbackState] = field(default_factory=list)
    disclosure: ToolDisclosureLevel = ToolDisclosureLevel.SUMMARY
    selected_tool_index: int = 0

    @property
    def entry_index(self) -> int:
        return self.tools[0].entry_index

    @property
    def selected_tool(self) -> ToolFeedbackState:
        selected = max(0, min(self.selected_tool_index, len(self.tools) - 1))
        return self.tools[selected]


@dataclass(slots=True)
class _ActiveToolInspector:
    """One modal Inspector bound to a live presentation-only tool state."""

    state: ToolFeedbackState
    group: ToolActivityGroupState
    screen: ToolInspectorScreen
    artifact_worker: Worker[None] | None = None


@dataclass(frozen=True, slots=True)
class ProviderSettingsSubmission:
    profile_name: str | None
    operation: str = "saved"


__all__ = [
    "TUI_RELOAD_PROVIDER_SETTINGS",
    "CollapsingPulseAnimation",
    "ProviderSettingsSubmission",
    "ToolActivityGroupState",
    "ToolFeedbackState",
    "TranscriptEntry",
]
