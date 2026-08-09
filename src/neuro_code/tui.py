from __future__ import annotations

import asyncio
import difflib
import logging
import os
import re
import sys
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any, ClassVar, Protocol, cast
from urllib.parse import urlsplit

from rich.console import RenderableType
from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.geometry import Size
from textual.message import Message as TextualMessage
from textual.screen import ModalScreen
from textual.suggester import Suggester
from textual.widgets import Button, Input, Label, Static
from textual.worker import Worker

from neuro_code.application.permissions.broker import ApprovalHandler
from neuro_code.application.permissions.contracts import PermissionApproval, PermissionRequest
from neuro_code.application.ports.http import HttpClientPolicy
from neuro_code.application.ports.provider_catalog import (
    ProviderCatalog,
    ProviderCatalogError,
    ProviderCatalogResult,
    ProviderConnectionSpec,
)
from neuro_code.application.ports.provider_settings import (
    ManagedProviderProfile,
    ManagedProviderSettings,
    ManagedProxyPolicy,
    ProviderSettingsStore,
)
from neuro_code.application.ports.ui_preferences import UiPreferencesStore
from neuro_code.application.providers.contracts import (
    ProviderOption,
    ProviderSelectionResult,
)
from neuro_code.application.providers.service import ChangeProviderRequest
from neuro_code.application.runtime.agent import AgentRunResult, EventSink
from neuro_code.application.sessions.contracts import (
    InteractionModeSelectionResult,
    ReasoningEffortSelectionResult,
    SessionOption,
)
from neuro_code.application.sessions.selection import (
    SessionSelectionController,
    SessionSelectionService,
)
from neuro_code.application.sessions.subagent_lifecycle import (
    SubagentRelationshipActionRequest,
    SubagentRelationshipLifecycleController,
)
from neuro_code.application.sessions.subagent_queries import (
    ListSubagentRelationshipsRequest,
    SubagentRelationshipAction,
    SubagentRelationshipQueryController,
)
from neuro_code.application.sessions.turns import RunTurnRequest, SessionTurnService
from neuro_code.application.tools.service import (
    ReadSessionToolOutputArtifactRequest,
    SessionToolOutputArtifactApplicationService,
)
from neuro_code.application.workflows.plan_execution import (
    ExecutePlanRequest,
    PlanExecutionService,
)
from neuro_code.application.workflows.plan_scheduling import (
    PlanSchedulingService,
    SchedulePlanRequest,
)
from neuro_code.application.workflows.session_task_execution import (
    QueuedPlanExecutionService,
    RunSessionTaskRequest,
)
from neuro_code.application.workflows.subagent import (
    ReadOnlySubagentApplicationService,
    RunSubagentRequest,
)
from neuro_code.configuration.app import resolve_http_client_policy
from neuro_code.domain.background_tasks.models import (
    BackgroundTaskSnapshot,
    BackgroundTaskStatus,
    BackgroundTaskWakePolicy,
    BackgroundWakeDecision,
    BackgroundWakeLimits,
    BackgroundWakeState,
)
from neuro_code.domain.conversation.context import estimate_context_tokens, estimate_text_tokens
from neuro_code.domain.conversation.events import AgentEvent, AgentEventKind
from neuro_code.domain.conversation.interaction_mode import InteractionMode
from neuro_code.domain.conversation.messages import Message, Role, SessionItem
from neuro_code.domain.conversation.reasoning import ReasoningEffort
from neuro_code.domain.execution import (
    AgentExecutionStatus,
    SessionExecutionRecord,
    TurnCancellationPolicy,
)
from neuro_code.domain.plans import PlanComment, PlanStepStatus, SessionPlan
from neuro_code.domain.session_tasks import SessionTask, SessionTaskKind, SessionTaskStatus
from neuro_code.interfaces.tui import recoverable_terminal_status
from neuro_code.shared.redaction import redact_sensitive_text
from neuro_code.shared.ui_language import UiLanguage
from neuro_code.tui_commands import SlashCompletion, slash_completions
from neuro_code.tui_text import language_name, ui_text
from neuro_code.tui_theme import (
    ACCENT_CODE,
    ACCENT_SUCCESS,
    ACCENT_WARNING,
    ASSISTANT_TEXT_STYLE,
    BRAND_TEXT,
    CONNECTION_STATUS_STYLES,
    DIFF_ADDITION_STYLE,
    DIFF_CONTEXT_STYLE,
    DIFF_DELETION_STYLE,
    DIFF_FILE_STYLE,
    DIFF_HUNK_STYLE,
    DIFF_SUMMARY_ADDITION_STYLE,
    DIFF_SUMMARY_DELETION_STYLE,
    EFFORT_STYLES,
    ERROR_DETAIL_STYLE,
    ERROR_LABEL_STYLE,
    ERROR_TEXT_STYLE,
    MARKDOWN_THEME,
    MODE_STYLES,
    MONO_SYNTAX_THEME,
    RECOVERABLE_LABEL_STYLE,
    RECOVERABLE_TEXT_STYLE,
    STATUS_LABEL_STYLE,
    STATUS_TEXT_STYLE,
    SYSTEM_LABEL_STYLE,
    SYSTEM_TEXT_STYLE,
    TEXT_BODY,
    TEXT_DIM,
    TEXT_DISABLED,
    TEXT_EMPHASIS,
    TEXT_MUTED,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
    TEXTUAL_THEME,
    TOOL_ACTIVE_STYLE,
    TOOL_COMPLETE_STYLE,
    TOOL_DETAIL_STYLE,
    TOOL_GUIDE_STYLE,
    TOOL_LABEL_STYLE,
    TOOL_META_STYLE,
    TOOL_TEXT_STYLE,
    TOOL_TITLE_STYLE,
    USER_TEXT_STYLE,
    WAITING_STYLE,
    loading_style,
)

_RESTORED_MESSAGE_LIMIT = 20_000
_TASK_LIST_LIMIT = 20
_TASK_POLL_SECONDS = 0.5
_MAX_QUEUED_INTERJECTIONS = 4
_DEFAULT_BACKGROUND_WAKE_LIMITS = BackgroundWakeLimits()
_TERMINAL_SIZE_POLL_SECONDS = 0.25
_LOADING_ANIMATION_TICK_SECONDS = 0.05
_TOOL_ELAPSED_UPDATE_SECONDS = 0.25
_COMMAND_HINT_LIMIT = 5
_TOOL_OUTPUT_MAX_LINES = 40
_TOOL_OUTPUT_MAX_CHARACTERS = 6_000
_TOOL_DIFF_MAX_FILES = 8
_COMPACT_READ_TOOLS = frozenset({"grep", "list_dir", "read_file", "skill", "view_image"})
TUI_RELOAD_PROVIDER_SETTINGS = 75
_PROMPT_MARK = "\N{SINGLE RIGHT-POINTING ANGLE QUOTATION MARK}"
_ERROR_MARK = "\N{MULTIPLICATION SIGN}"
_SUCCESS_MARK = "\N{CHECK MARK}"
_WARNING_MARK = "!"
_ANSI_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_LOGGER = logging.getLogger(__name__)


def _markdown_code_theme() -> str:
    """Contain Rich Markdown's narrow annotation without changing the runtime theme.

    ``Markdown`` forwards the value to ``Syntax``, whose runtime API accepts a
    ``PygmentsSyntaxTheme``. Its public annotation is limited to a named string
    theme, so this local cast preserves the custom theme object.

    通过局部类型辅助函数容纳 Rich Markdown 的窄类型注解,不改变运行时主题.
    """

    return cast(str, MONO_SYNTAX_THEME)


@dataclass(frozen=True, slots=True)
class ProviderPreset:
    name: str
    protocol: str
    dialect: str
    base_url: str


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


_PROVIDER_PRESETS: tuple[ProviderPreset, ...] = (
    ProviderPreset("openai", "openai-responses", "standard", "https://api.openai.com/v1"),
    ProviderPreset("compatible", "openai-chat", "standard", ""),
    ProviderPreset("deepseek", "openai-chat", "deepseek-v4", "https://api.deepseek.com"),
    ProviderPreset("anthropic", "anthropic-messages", "standard", "https://api.anthropic.com"),
    ProviderPreset(
        "gemini",
        "gemini-generate-content",
        "standard",
        "https://generativelanguage.googleapis.com/v1beta",
    ),
    ProviderPreset("xai", "openai-responses", "xai", "https://api.x.ai/v1"),
)


def _is_deepseek_base_url(value: str) -> bool:
    try:
        return urlsplit(value.strip()).hostname == "api.deepseek.com"
    except ValueError:
        return False


def _read_terminal_size() -> Size | None:
    """Read the real TTY viewport without trusting possibly stale shell variables.

    读取真实 TTY 视口,不依赖可能过期的 shell 变量."""

    for stream in (sys.__stdin__, sys.__stderr__, sys.__stdout__):
        if stream is None:
            continue
        try:
            terminal_size = os.get_terminal_size(stream.fileno())
        except (AttributeError, OSError, ValueError):
            continue
        if terminal_size.columns > 0 and terminal_size.lines > 0:
            return Size(terminal_size.columns, terminal_size.lines)
    return None


class ConversationRunner(Protocol):
    @property
    def session_id(self) -> str | None: ...

    async def run(
        self,
        prompt: str,
        *,
        sink: EventSink | None = None,
        cancellation_policy: TurnCancellationPolicy = TurnCancellationPolicy.RETAIN,
    ) -> AgentRunResult: ...

    async def run_background_wake(self, *, sink: EventSink | None = None) -> AgentRunResult: ...

    async def load_background_wake_state(self) -> BackgroundWakeState: ...

    async def save_background_wake_state(self, state: BackgroundWakeState) -> None: ...


class ApprovalController(Protocol):
    def set_handler(self, handler: ApprovalHandler | None) -> None: ...


class ProviderController(Protocol):
    @property
    def profiles(self) -> tuple[ProviderOption, ...]: ...

    @property
    def selected_profile(self) -> str: ...

    async def change_provider(self, request: ChangeProviderRequest) -> ProviderSelectionResult: ...


class ReasoningController(Protocol):
    @property
    def reasoning_effort(self) -> ReasoningEffort: ...

    @property
    def effective_reasoning_effort(self) -> ReasoningEffort: ...

    async def set_reasoning_effort(
        self,
        effort: ReasoningEffort,
    ) -> ReasoningEffortSelectionResult: ...


class InteractionModeController(Protocol):
    @property
    def interaction_mode(self) -> InteractionMode: ...

    @property
    def auto_mode_unrestricted(self) -> bool: ...

    async def set_interaction_mode(
        self,
        mode: InteractionMode,
    ) -> InteractionModeSelectionResult: ...


SessionController = SessionSelectionController


SessionSearchCallback = Callable[[str | None], Awaitable[tuple[SessionOption, ...]]]


class TaskController(Protocol):
    async def list_background_tasks(self) -> tuple[BackgroundTaskSnapshot, ...]: ...

    async def load_background_wake_state(self) -> BackgroundWakeState: ...

    async def save_background_wake_state(self, state: BackgroundWakeState) -> None: ...


class SessionTaskController(Protocol):
    async def list_session_tasks(self) -> tuple[SessionTask, ...]: ...

    async def get_session_task(self, task_id: str) -> SessionTask | None: ...


class PlanController(Protocol):
    @property
    def plan(self) -> SessionPlan | None: ...

    async def add_plan_comment(self, step_index: int, content: str) -> PlanComment: ...

    async def list_plan_comments(self) -> tuple[PlanComment, ...]: ...

    async def schedule_plan(self) -> SessionTask: ...

    async def execute_plan(
        self,
        *,
        sink: EventSink | None = None,
        task_id: str | None = None,
    ) -> AgentRunResult: ...

    async def run_session_task(
        self,
        task_id: str,
        *,
        sink: EventSink | None = None,
    ) -> AgentRunResult: ...


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
    started_at: float | None = None
    content: str | None = None
    is_error: bool = False
    metadata: dict[str, Any] | None = None
    workspace_changes: dict[str, Any] | None = None
    artifact_id: str | None = None
    artifact_content: str | None = None
    artifact_read_truncated: bool = False
    artifact_loading: bool = False
    artifact_unavailable: bool = False
    expanded: bool = False


@dataclass(frozen=True, slots=True)
class ProviderSettingsSubmission:
    profile_name: str | None
    operation: str = "saved"


class AssistantMarkdown(Markdown):
    """Safe model Markdown whose string form remains useful in diagnostics.

    安全的模型 Markdown,其字符串形式仍适合诊断."""

    def __str__(self) -> str:
        return self.markup


class SlashCommandSuggester(Suggester):
    """Show the same first completion that Tab will apply.

    显示 Tab 键将应用的第一条补全结果."""

    def __init__(
        self,
        completions: Callable[[str], tuple[SlashCompletion, ...]],
    ) -> None:
        super().__init__(use_cache=False, case_sensitive=False)
        self._completions = completions

    async def get_suggestion(self, value: str) -> str | None:
        completions = self._completions(value)
        if not completions or completions[0].value == value:
            return None
        return completions[0].value


class ConversationMessage(Static):
    """One stable message node in the scrollable conversation.

    可滚动会话中的一个稳定消息节点."""

    def __init__(
        self,
        category: str,
        rendered: RenderableType,
        *,
        pending: bool = False,
    ) -> None:
        classes = f"conversation-message message-{category}"
        if pending:
            classes += " message-pending"
        super().__init__(rendered, markup=False, classes=classes)
        self.category = category

    def set_pending(self, pending: bool) -> None:
        self.set_class(pending, "message-pending")


class PromptInput(Input):
    """Prompt input that preserves all lines from a terminal paste.

    保留终端粘贴内容中全部行的提示输入框.

    Textual's single-line ``Input`` intentionally keeps only the first line of a
    bracketed paste. The prompt still submits through the same ``Input`` message
    contract, but pasted newlines remain in its value so multi-line prompts are
    not silently truncated.
    Textual 的单行 ``Input`` 默认只保留 bracketed paste 的第一行.提示框仍沿用
    原有 ``Input`` 消息契约,但会保留粘贴值中的换行,避免多行提示被静默截断.
    """

    @property
    def _value(self) -> Text:
        """Render pasted newlines without allowing them to escape the input row.

        将粘贴内容中的换行渲染为单字符标记,避免突破单行输入框的布局.
        """

        if self.password:
            return Text("•" * len(self.value), no_wrap=True, overflow="ignore", end="")
        text = Text(self.value.replace("\n", "↵"), no_wrap=True, overflow="ignore", end="")
        if self.highlighter is not None:
            text = self.highlighter(text)
        return text

    def _on_paste(self, event: events.Paste) -> None:
        text = event.text.replace("\r\n", "\n").replace("\r", "\n")
        if text:
            start, end = self.selection
            if start == end:
                self.insert_text_at_cursor(text)
            else:
                self.replace(text, start, end)
        event.prevent_default().stop()


class ToolFeedbackMessage(ConversationMessage, can_focus=True):
    """A stable tool card whose already-bounded details can be toggled.

    可切换已限制详情显示的稳定工具卡片."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("enter", "toggle_details", "Toggle details", show=False),
        Binding("space", "toggle_details", "Toggle details", show=False),
    ]

    class ToggleRequested(TextualMessage):
        """Request that the owning app toggle one tool card.

        请求所属应用切换一个工具卡片."""

        def __init__(self, card: ToolFeedbackMessage) -> None:
            self.card = card
            super().__init__()

    def __init__(self, rendered: RenderableType, *, entry_index: int) -> None:
        super().__init__("tool", rendered)
        self.entry_index = entry_index

    async def _on_click(self, event: events.Click) -> None:
        event.stop()
        self.focus()
        self.post_message(self.ToggleRequested(self))

    def action_toggle_details(self) -> None:
        self.post_message(self.ToggleRequested(self))


class SettingsScreen(ModalScreen[str | None]):
    """First-level settings navigation; detailed forms live on child screens.

    一级设置导航;详细表单位于子界面."""

    CSS = """
    SettingsScreen {
        align: center middle;
        background: $background 85%;
    }

    #settings-dialog {
        width: 82%;
        max-width: 78;
        height: auto;
        max-height: 85%;
        padding: 1 2;
        border: solid $border;
        background: $surface;
    }

    #settings-title {
        text-style: bold;
        color: $text-primary;
        margin-bottom: 1;
    }

    #settings-description {
        color: $text-muted;
        margin-bottom: 1;
    }

    #settings-categories {
        height: auto;
    }

    #settings-categories Button {
        width: 100%;
        margin-bottom: 1;
        content-align: left middle;
    }

    #settings-help {
        color: $text-muted;
    }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("ctrl+c", "cancel", "Cancel", show=False),
    ]

    def __init__(
        self,
        selected: UiLanguage,
        *,
        language: UiLanguage,
        provider_settings_available: bool,
    ) -> None:
        super().__init__()
        self.selected = selected
        self.language = language
        self.provider_settings_available = provider_settings_available

    def compose(self) -> ComposeResult:
        language_summary = language_name(self.selected, in_language=self.language)
        yield Vertical(
            Label(ui_text(self.language, "settings.title"), id="settings-title"),
            Static(ui_text(self.language, "settings.description"), id="settings-description"),
            Vertical(
                Button(
                    ui_text(
                        self.language,
                        "settings.category.language",
                        current=language_summary,
                    ),
                    id="settings-category-language",
                    variant="primary",
                ),
                Button(
                    ui_text(self.language, "settings.category.providers"),
                    id="settings-category-providers",
                    disabled=not self.provider_settings_available,
                ),
                Button(
                    ui_text(self.language, "settings.category.network"),
                    id="settings-category-network",
                    disabled=not self.provider_settings_available,
                ),
                Button(
                    ui_text(self.language, "settings.category.background_wake"),
                    id="settings-category-background-wake",
                    disabled=not self.provider_settings_available,
                ),
                id="settings-categories",
            ),
            Static(ui_text(self.language, "settings.help"), id="settings-help"),
            id="settings-dialog",
        )

    def on_mount(self) -> None:
        self.query_one("#settings-category-language", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        categories = {
            "settings-category-language": "language",
            "settings-category-providers": "providers",
            "settings-category-network": "network",
            "settings-category-background-wake": "background-wake",
        }
        category = categories.get(event.button.id or "")
        if category is not None:
            self.dismiss(category)

    def action_cancel(self) -> None:
        self.dismiss(None)


class LanguageSettingsScreen(ModalScreen[UiLanguage | None]):
    """Edit one interface preference without rendering unrelated provider fields.

    编辑一项界面偏好,不渲染无关的 Provider 字段."""

    CSS = """
    LanguageSettingsScreen {
        align: center middle;
        background: $background 85%;
    }

    #language-settings-dialog {
        width: 82%;
        max-width: 78;
        height: auto;
        padding: 1 2;
        border: solid $border;
        background: $surface;
    }

    #language-settings-title {
        text-style: bold;
        color: $text-primary;
        margin-bottom: 1;
    }

    #language-settings-description,
    #language-settings-help {
        color: $text-muted;
        margin-bottom: 1;
    }

    #settings-languages,
    #language-settings-actions {
        height: auto;
    }

    #settings-languages Button {
        width: 1fr;
        margin-right: 1;
    }

    #language-settings-actions {
        align-horizontal: right;
    }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Back", show=False),
        Binding("ctrl+c", "cancel", "Back", show=False),
    ]

    def __init__(self, selected: UiLanguage, *, language: UiLanguage) -> None:
        super().__init__()
        self.selected = selected
        self.language = language

    def _choice_label(self, choice: UiLanguage) -> str:
        label = language_name(choice, in_language=choice)
        if choice is self.selected:
            label += f" · {ui_text(self.language, 'settings.current')}"
        return label

    def compose(self) -> ComposeResult:
        yield Vertical(
            Label(
                ui_text(self.language, "settings.language.title"),
                id="language-settings-title",
            ),
            Static(
                ui_text(self.language, "settings.language.description"),
                id="language-settings-description",
            ),
            Horizontal(
                Button(
                    self._choice_label(UiLanguage.SIMPLIFIED_CHINESE),
                    id="settings-language-zh-cn",
                    variant=(
                        "primary" if self.selected is UiLanguage.SIMPLIFIED_CHINESE else "default"
                    ),
                ),
                Button(
                    self._choice_label(UiLanguage.ENGLISH),
                    id="settings-language-en",
                    variant="primary" if self.selected is UiLanguage.ENGLISH else "default",
                ),
                id="settings-languages",
            ),
            Static(
                ui_text(self.language, "settings.language.help"),
                id="language-settings-help",
            ),
            Horizontal(
                Button(ui_text(self.language, "settings.back"), id="language-settings-back"),
                id="language-settings-actions",
            ),
            id="language-settings-dialog",
        )

    def on_mount(self) -> None:
        selector = (
            "#settings-language-zh-cn"
            if self.selected is UiLanguage.SIMPLIFIED_CHINESE
            else "#settings-language-en"
        )
        self.query_one(selector, Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        choices = {
            "settings-language-zh-cn": UiLanguage.SIMPLIFIED_CHINESE,
            "settings-language-en": UiLanguage.ENGLISH,
        }
        choice = choices.get(event.button.id or "")
        if choice is not None:
            self.dismiss(choice)
        elif event.button.id == "language-settings-back":
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class NetworkProxySettingsScreen(ModalScreen[ManagedProviderSettings | None]):
    """Edit the user-wide proxy default independently of provider credentials.

    独立编辑用户级代理默认值,不涉及 Provider 凭据."""

    CSS = """
    NetworkProxySettingsScreen {
        align: center middle;
        background: $background 85%;
    }

    #network-settings-dialog {
        width: 82%;
        max-width: 88;
        height: auto;
        padding: 1 2;
        border: solid $border;
        background: $surface;
    }

    #network-settings-title {
        text-style: bold;
        color: $text-primary;
        margin-bottom: 1;
    }

    #network-settings-description,
    #network-settings-hint,
    #network-settings-error {
        color: $text-muted;
        margin-bottom: 1;
    }

    #network-settings-error {
        padding-left: 1;
        border-left: tall $border-focus;
        color: $text-primary;
        text-style: bold;
    }

    #network-settings-modes,
    #network-settings-actions {
        height: auto;
    }

    #network-settings-modes Button {
        width: 1fr;
        margin-right: 1;
    }

    #network-settings-actions {
        align-horizontal: right;
    }

    #network-settings-actions Button {
        margin-left: 1;
    }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Back", show=False),
        Binding("ctrl+c", "cancel", "Back", show=False),
    ]

    def __init__(
        self,
        *,
        language: UiLanguage,
        provider_settings: ManagedProviderSettings,
        provider_settings_store: ProviderSettingsStore,
    ) -> None:
        super().__init__()
        self.language = language
        self.provider_settings = provider_settings
        self.provider_settings_store = provider_settings_store
        self._active_proxy_mode = provider_settings.proxy_defaults.mode

    def compose(self) -> ComposeResult:
        yield Vertical(
            Label(ui_text(self.language, "network_settings.title"), id="network-settings-title"),
            Static(
                ui_text(self.language, "network_settings.description"),
                id="network-settings-description",
            ),
            Label(ui_text(self.language, "network_settings.default_policy")),
            Horizontal(
                Button(
                    ui_text(self.language, "network_settings.environment"),
                    id="network-settings-environment",
                    variant="primary",
                ),
                Button(
                    ui_text(self.language, "network_settings.direct"),
                    id="network-settings-direct",
                ),
                Button(
                    ui_text(self.language, "network_settings.explicit"),
                    id="network-settings-explicit",
                ),
                id="network-settings-modes",
            ),
            Input(
                value=self.provider_settings.proxy_defaults.proxy_url_env or "",
                placeholder=ui_text(self.language, "network_settings.environment_variable"),
                id="network-settings-proxy-env",
                disabled=self.provider_settings.proxy_defaults.mode != "explicit",
            ),
            Static("", id="network-settings-hint"),
            Static("", id="network-settings-error"),
            Horizontal(
                Button(ui_text(self.language, "settings.back"), id="network-settings-back"),
                Button(
                    ui_text(self.language, "network_settings.save"),
                    id="network-settings-save",
                    variant="success",
                ),
                id="network-settings-actions",
            ),
            id="network-settings-dialog",
        )

    def on_mount(self) -> None:
        self._select_proxy_mode(self._active_proxy_mode)
        self.query_one(f"#network-settings-{self._active_proxy_mode}", Button).focus()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id.startswith("network-settings-"):
            mode = button_id.removeprefix("network-settings-")
            if mode in {"environment", "direct", "explicit"}:
                self._select_proxy_mode(mode)
                return
        if button_id == "network-settings-save":
            await self._save()
        elif button_id == "network-settings-back":
            self.dismiss(None)

    def _select_proxy_mode(self, proxy_mode: str) -> None:
        if proxy_mode not in {"environment", "direct", "explicit"}:
            return
        self._active_proxy_mode = proxy_mode
        for candidate in ("environment", "direct", "explicit"):
            button = self.query_one(f"#network-settings-{candidate}", Button)
            button.variant = "primary" if candidate == proxy_mode else "default"
        self.query_one("#network-settings-proxy-env", Input).disabled = proxy_mode != "explicit"
        self.query_one("#network-settings-hint", Static).update(
            ui_text(self.language, f"network_settings.hint.{proxy_mode}")
        )

    async def _save(self) -> None:
        proxy_url_env = (
            self.query_one("#network-settings-proxy-env", Input).value.strip() or None
            if self._active_proxy_mode == "explicit"
            else None
        )
        try:
            proxy_defaults = ManagedProxyPolicy(self._active_proxy_mode, proxy_url_env)
            resolve_http_client_policy(
                proxy_mode=proxy_defaults.mode,
                proxy_url_env=proxy_defaults.proxy_url_env,
                environ=os.environ,
            )
            settings = await self.provider_settings_store.save_proxy_defaults(proxy_defaults)
        except Exception as error:
            self.query_one("#network-settings-error", Static).update(
                Text(f"{_ERROR_MARK} {error}", style=ERROR_TEXT_STYLE)
            )
            return
        self.dismiss(settings)

    def action_cancel(self) -> None:
        self.dismiss(None)


class BackgroundWakeSettingsScreen(ModalScreen[ManagedProviderSettings | None]):
    """Edit the user-wide background-task wake default.

    编辑用户级后台任务唤醒默认值."""

    CSS = """
    BackgroundWakeSettingsScreen {
        align: center middle;
        background: $background 85%;
    }

    #background-wake-settings-dialog {
        width: 82%;
        max-width: 88;
        height: auto;
        padding: 1 2;
        border: solid $border;
        background: $surface;
    }

    #background-wake-settings-title {
        text-style: bold;
        color: $text-primary;
        margin-bottom: 1;
    }

    #background-wake-settings-description,
    #background-wake-settings-hint,
    #background-wake-settings-error {
        color: $text-muted;
        margin-bottom: 1;
    }

    #background-wake-settings-error {
        padding-left: 1;
        border-left: tall $border-focus;
        color: $text-primary;
        text-style: bold;
    }

    #background-wake-settings-modes,
    #background-wake-settings-actions {
        height: auto;
    }

    #background-wake-settings-modes Button {
        width: 1fr;
        margin-right: 1;
    }

    #background-wake-settings-actions {
        align-horizontal: right;
    }

    #background-wake-settings-actions Button {
        margin-left: 1;
    }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Back", show=False),
        Binding("ctrl+c", "cancel", "Back", show=False),
    ]

    def __init__(
        self,
        *,
        language: UiLanguage,
        provider_settings: ManagedProviderSettings,
        provider_settings_store: ProviderSettingsStore,
    ) -> None:
        super().__init__()
        self.language = language
        self.provider_settings = provider_settings
        self.provider_settings_store = provider_settings_store
        self._active_policy = provider_settings.background_task_wake_policy

    def compose(self) -> ComposeResult:
        yield Vertical(
            Label(
                ui_text(self.language, "background_wake_settings.title"),
                id="background-wake-settings-title",
            ),
            Static(
                ui_text(self.language, "background_wake_settings.description"),
                id="background-wake-settings-description",
            ),
            Label(ui_text(self.language, "background_wake_settings.default_policy")),
            Horizontal(
                Button(
                    ui_text(self.language, "background_wake_settings.disabled"),
                    id="background-wake-settings-disabled",
                ),
                Button(
                    ui_text(self.language, "background_wake_settings.enabled"),
                    id="background-wake-settings-enabled",
                ),
                id="background-wake-settings-modes",
            ),
            Static("", id="background-wake-settings-hint"),
            Static("", id="background-wake-settings-error"),
            Horizontal(
                Button(ui_text(self.language, "settings.back"), id="background-wake-settings-back"),
                Button(
                    ui_text(self.language, "background_wake_settings.save"),
                    id="background-wake-settings-save",
                    variant="success",
                ),
                id="background-wake-settings-actions",
            ),
            id="background-wake-settings-dialog",
        )

    def on_mount(self) -> None:
        self._select_policy(self._active_policy)
        self.query_one(f"#background-wake-settings-{self._active_policy.value}", Button).focus()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id in {"background-wake-settings-disabled", "background-wake-settings-enabled"}:
            self._select_policy(
                BackgroundTaskWakePolicy(button_id.removeprefix("background-wake-settings-"))
            )
        elif button_id == "background-wake-settings-save":
            try:
                settings = await self.provider_settings_store.save_background_task_wake_policy(
                    self._active_policy
                )
            except Exception as error:
                self.query_one("#background-wake-settings-error", Static).update(
                    Text(f"{_ERROR_MARK} {error}", style=ERROR_TEXT_STYLE)
                )
                return
            self.dismiss(settings)
        elif button_id == "background-wake-settings-back":
            self.dismiss(None)

    def _select_policy(self, policy: BackgroundTaskWakePolicy) -> None:
        self._active_policy = policy
        for candidate in BackgroundTaskWakePolicy:
            self.query_one(f"#background-wake-settings-{candidate.value}", Button).variant = (
                "primary" if candidate is policy else "default"
            )
        self.query_one("#background-wake-settings-hint", Static).update(
            ui_text(
                self.language,
                f"background_wake_settings.hint.{policy.value}",
            )
        )

    def action_cancel(self) -> None:
        self.dismiss(None)


class ProviderSettingsScreen(ModalScreen[ProviderSettingsSubmission | None]):
    """Create and edit user-owned provider profiles on a focused detail screen.

    在聚焦的详情界面创建和编辑用户拥有的 Provider 配置."""

    CSS = """
    ProviderSettingsScreen {
        align: center middle;
        background: $background 85%;
    }

    #provider-settings-dialog {
        width: 90%;
        max-width: 105;
        height: 95%;
        max-height: 95%;
        padding: 1 2;
        border: solid $border;
        background: $surface;
    }

    #provider-settings-content {
        height: 1fr;
    }

    #provider-settings-title {
        text-style: bold;
        color: $text-primary;
        margin-bottom: 1;
    }

    #provider-settings-description,
    #provider-settings-protocol-hint,
    #provider-settings-proxy-title,
    #provider-settings-proxy-hint,
    #provider-settings-context-hint,
    #provider-settings-connection-status,
    #provider-settings-error,
    #provider-settings-empty {
        color: $text-muted;
        margin-bottom: 1;
    }

    #provider-settings-protocol-hint {
        color: $text-secondary;
    }

    #provider-settings-proxy-title {
        color: $text;
        text-style: bold;
        margin-top: 1;
    }

    #provider-settings-proxy-hint {
        color: $text-secondary;
    }

    #provider-settings-connection-status {
        margin-top: 1;
        margin-bottom: 1;
    }

    #provider-settings-error {
        padding-left: 1;
        border-left: tall $border-focus;
        color: $text-primary;
        text-style: bold;
    }

    #provider-settings-profiles {
        height: auto;
        max-height: 8;
        margin-bottom: 1;
    }

    #provider-settings-models {
        display: none;
        height: auto;
        max-height: 10;
        margin-bottom: 1;
    }

    #provider-settings-models Button {
        width: 100%;
        margin-bottom: 1;
    }

    #provider-settings-profiles Button {
        width: 100%;
        margin-bottom: 1;
    }

    #provider-settings-presets,
    #provider-settings-presets-row-one,
    #provider-settings-presets-row-two,
    #provider-settings-proxy-modes,
    #provider-settings-wake-modes,
    #provider-settings-form,
    #provider-settings-actions {
        height: auto;
    }

    #provider-settings-presets {
        margin-bottom: 1;
    }

    #provider-settings-presets Button {
        width: 1fr;
        margin-right: 1;
    }

    #provider-settings-presets-row-one {
        margin-bottom: 1;
    }

    #provider-settings-form Input {
        margin-bottom: 0;
    }

    #provider-settings-form Label {
        color: $text-primary;
        margin-top: 1;
    }

    #provider-settings-proxy-modes Button {
        width: 1fr;
        margin-right: 1;
    }

    #provider-settings-wake-modes Button {
        width: 1fr;
        margin-right: 1;
    }

    #provider-settings-form {
        margin-bottom: 1;
    }

    #provider-settings-actions {
        align-horizontal: right;
    }

    #provider-settings-actions Button {
        margin-left: 1;
    }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Back", show=False),
        Binding("ctrl+c", "cancel", "Back", show=False),
    ]

    def __init__(
        self,
        *,
        language: UiLanguage,
        provider_settings: ManagedProviderSettings,
        provider_settings_store: ProviderSettingsStore,
        provider_catalog: ProviderCatalog | None = None,
        first_run: bool = False,
        initial_profile: str | None = None,
        initial_error: str | None = None,
    ) -> None:
        super().__init__()
        self.language = language
        self.provider_settings = provider_settings
        self.provider_settings_store = provider_settings_store
        self.provider_catalog = provider_catalog
        self.first_run = first_run
        self.initial_profile = initial_profile
        self.initial_error = initial_error
        self._editing_profile: str | None = None
        self._active_preset = "openai"
        self._active_proxy_mode: str | None = None
        self._active_background_wake_policy: BackgroundTaskWakePolicy | None = None
        self._delete_confirmation_for: str | None = None
        self._catalog_model_ids: dict[str, str] = {}
        self._profile_ids = {
            f"provider-settings-profile-{index}": profile.name
            for index, profile in enumerate(provider_settings.profiles)
        }

    def compose(self) -> ComposeResult:
        profile_widgets: list[Any] = [
            Button(
                self._provider_label(profile),
                id=f"provider-settings-profile-{index}",
                variant=(
                    "primary"
                    if profile.name == self.provider_settings.default_provider
                    else "default"
                ),
            )
            for index, profile in enumerate(self.provider_settings.profiles)
        ]
        if not profile_widgets:
            profile_widgets.append(
                Static(
                    ui_text(self.language, "provider_settings.empty"), id="provider-settings-empty"
                )
            )
        preset_buttons = [
            Button(
                ui_text(self.language, f"provider_settings.preset.{preset.name}"),
                id=f"provider-settings-preset-{preset.name}",
                variant="primary" if preset.name == self._active_preset else "default",
            )
            for preset in _PROVIDER_PRESETS
        ]
        actions: list[Any] = []
        if not self.first_run:
            actions.extend(
                (
                    Button(ui_text(self.language, "settings.back"), id="provider-settings-back"),
                    Button(
                        ui_text(self.language, "provider_settings.delete"),
                        id="provider-settings-delete",
                        disabled=True,
                    ),
                )
            )
        actions.extend(
            (
                Button(
                    ui_text(self.language, "provider_settings.new"),
                    id="provider-settings-new",
                ),
                Button(
                    ui_text(self.language, "provider_settings.connection.test"),
                    id="provider-settings-test",
                    disabled=self.provider_catalog is None,
                ),
                Button(
                    ui_text(self.language, "provider_settings.save_use"),
                    id="provider-settings-save",
                    variant="success",
                ),
            )
        )
        yield Vertical(
            VerticalScroll(
                Label(
                    ui_text(
                        self.language,
                        "provider_settings.first_run_title"
                        if self.first_run
                        else "provider_settings.title",
                    ),
                    id="provider-settings-title",
                ),
                Static(
                    ui_text(self.language, "provider_settings.description"),
                    id="provider-settings-description",
                ),
                VerticalScroll(*profile_widgets, id="provider-settings-profiles"),
                Vertical(
                    Horizontal(*preset_buttons[:3], id="provider-settings-presets-row-one"),
                    Horizontal(*preset_buttons[3:], id="provider-settings-presets-row-two"),
                    id="provider-settings-presets",
                ),
                Static(
                    ui_text(self.language, "provider_settings.protocol.openai"),
                    id="provider-settings-protocol-hint",
                ),
                Vertical(
                    Label(ui_text(self.language, "provider_settings.field.name")),
                    Input(
                        placeholder=ui_text(self.language, "provider_settings.name"),
                        id="provider-settings-name",
                    ),
                    Label(ui_text(self.language, "provider_settings.field.model")),
                    Input(
                        placeholder=ui_text(self.language, "provider_settings.model.openai"),
                        id="provider-settings-model",
                    ),
                    Label(ui_text(self.language, "provider_settings.field.base_url")),
                    Input(
                        value="https://api.openai.com/v1",
                        placeholder=ui_text(self.language, "provider_settings.base_url"),
                        id="provider-settings-base-url",
                    ),
                    Label(ui_text(self.language, "provider_settings.field.api_key")),
                    Input(
                        placeholder=ui_text(self.language, "provider_settings.api_key"),
                        password=True,
                        id="provider-settings-api-key",
                    ),
                    Label(ui_text(self.language, "provider_settings.field.context_window")),
                    Input(
                        placeholder=ui_text(self.language, "provider_settings.context_window"),
                        id="provider-settings-context-window",
                    ),
                    Static(
                        ui_text(self.language, "provider_settings.context_window_hint"),
                        id="provider-settings-context-hint",
                    ),
                    Static(
                        ui_text(self.language, "provider_settings.proxy.title"),
                        id="provider-settings-proxy-title",
                    ),
                    Horizontal(
                        Button(
                            ui_text(self.language, "provider_settings.proxy.inherit"),
                            id="provider-settings-proxy-inherit",
                            variant="primary",
                        ),
                        Button(
                            ui_text(self.language, "provider_settings.proxy.environment"),
                            id="provider-settings-proxy-environment",
                        ),
                        Button(
                            ui_text(self.language, "provider_settings.proxy.direct"),
                            id="provider-settings-proxy-direct",
                        ),
                        Button(
                            ui_text(self.language, "provider_settings.proxy.explicit"),
                            id="provider-settings-proxy-explicit",
                        ),
                        id="provider-settings-proxy-modes",
                    ),
                    Input(
                        placeholder=ui_text(
                            self.language,
                            "provider_settings.proxy.environment_variable",
                        ),
                        id="provider-settings-proxy-env",
                        disabled=True,
                    ),
                    Static(
                        ui_text(
                            self.language,
                            "provider_settings.proxy.hint.environment",
                        ),
                        id="provider-settings-proxy-hint",
                    ),
                    Static(
                        ui_text(
                            self.language,
                            "provider_settings.background_wake.title",
                        ),
                        id="provider-settings-background-wake-title",
                    ),
                    Horizontal(
                        Button(
                            ui_text(
                                self.language,
                                "provider_settings.background_wake.inherit",
                            ),
                            id="provider-settings-wake-inherit",
                            variant="primary",
                        ),
                        Button(
                            ui_text(
                                self.language,
                                "provider_settings.background_wake.disabled",
                            ),
                            id="provider-settings-wake-disabled",
                        ),
                        Button(
                            ui_text(
                                self.language,
                                "provider_settings.background_wake.enabled",
                            ),
                            id="provider-settings-wake-enabled",
                        ),
                        id="provider-settings-wake-modes",
                    ),
                    Static(
                        ui_text(
                            self.language,
                            "provider_settings.background_wake.hint",
                        ),
                        id="provider-settings-background-wake-hint",
                    ),
                    Static("", id="provider-settings-connection-status"),
                    VerticalScroll(id="provider-settings-models"),
                    id="provider-settings-form",
                ),
                Static("", id="provider-settings-error"),
                id="provider-settings-content",
            ),
            Horizontal(*actions, id="provider-settings-actions"),
            id="provider-settings-dialog",
        )

    def _provider_label(self, profile: ManagedProviderProfile) -> str:
        suffix = (
            f" · {ui_text(self.language, 'marker.default')}"
            if profile.name == self.provider_settings.default_provider
            else ""
        )
        return f"{profile.name} · {profile.model}{suffix}"

    def on_mount(self) -> None:
        if self.initial_profile is not None:
            self._edit_profile(self.initial_profile)
        if self.initial_error:
            self._show_provider_error(self.initial_error)
        focus_target = (
            "#provider-settings-model"
            if self._editing_profile is not None
            else "#provider-settings-name"
        )
        self.query_one(focus_target, Input).focus()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        profile_name = self._profile_ids.get(button_id)
        if profile_name is not None:
            self._edit_profile(profile_name)
            return
        catalog_model = self._catalog_model_ids.get(button_id)
        if catalog_model is not None:
            self.query_one("#provider-settings-model", Input).value = catalog_model
            self._show_connection_status(
                ui_text(
                    self.language,
                    "provider_settings.connection.selected",
                    model=catalog_model,
                ),
                kind="success",
            )
            return
        if button_id.startswith("provider-settings-preset-"):
            self._select_preset(button_id.removeprefix("provider-settings-preset-"))
            return
        if button_id.startswith("provider-settings-proxy-"):
            selection = button_id.removeprefix("provider-settings-proxy-")
            self._select_proxy_mode(None if selection == "inherit" else selection)
            return
        if button_id.startswith("provider-settings-wake-"):
            selection = button_id.removeprefix("provider-settings-wake-")
            self._select_background_wake_policy(
                None if selection == "inherit" else BackgroundTaskWakePolicy(selection)
            )
            return
        if button_id == "provider-settings-new":
            self._new_profile()
            return
        if button_id == "provider-settings-save":
            await self._save_provider()
            return
        if button_id == "provider-settings-test":
            self.run_worker(
                self._test_connection(),
                name="provider-model-discovery",
                group="provider-model-discovery",
                exclusive=True,
                exit_on_error=False,
            )
            return
        if button_id == "provider-settings-delete":
            await self._delete_provider()
            return
        if button_id == "provider-settings-back":
            self.dismiss(None)

    def _edit_profile(self, name: str) -> None:
        profile = self.provider_settings.profile(name)
        if profile is None:
            return
        self._editing_profile = name
        self._clear_model_catalog()
        name_input = self.query_one("#provider-settings-name", Input)
        name_input.value = profile.name
        name_input.disabled = True
        self.query_one("#provider-settings-model", Input).value = profile.model
        self.query_one("#provider-settings-base-url", Input).value = profile.base_url
        self.query_one("#provider-settings-api-key", Input).value = ""
        self.query_one("#provider-settings-context-window", Input).value = (
            str(profile.context_window_tokens) if profile.context_window_tokens is not None else ""
        )
        self._select_preset(self._preset_for_profile(profile), update_endpoint=False)
        self.query_one("#provider-settings-proxy-env", Input).value = profile.proxy_url_env or ""
        self._select_proxy_mode(profile.proxy_mode)
        self._select_background_wake_policy(profile.background_task_wake_policy)
        self._reset_delete_confirmation()
        if not self.first_run:
            self.query_one("#provider-settings-delete", Button).disabled = False
        self._show_provider_error("")
        self.query_one("#provider-settings-model", Input).focus()

    @staticmethod
    def _preset_for_profile(profile: ManagedProviderProfile) -> str:
        base_url = profile.base_url.rstrip("/").casefold()
        if _is_deepseek_base_url(profile.base_url):
            if profile.protocol == "openai-chat" and profile.dialect == "deepseek-v4":
                return "deepseek"
            if profile.protocol == "openai-responses" and profile.dialect == "standard":
                return "openai"
        exact = next(
            (
                preset.name
                for preset in _PROVIDER_PRESETS
                if preset.base_url
                and base_url == preset.base_url.rstrip("/").casefold()
                and preset.protocol == profile.protocol
                and preset.dialect == profile.dialect
            ),
            None,
        )
        if exact is not None:
            return exact
        return next(
            (
                preset.name
                for preset in _PROVIDER_PRESETS
                if preset.protocol == profile.protocol and preset.dialect == profile.dialect
            ),
            "compatible",
        )

    def _new_profile(self) -> None:
        self._editing_profile = None
        self._clear_model_catalog()
        name_input = self.query_one("#provider-settings-name", Input)
        name_input.disabled = False
        name_input.value = ""
        self.query_one("#provider-settings-model", Input).value = ""
        self.query_one("#provider-settings-api-key", Input).value = ""
        self.query_one("#provider-settings-context-window", Input).value = ""
        self.query_one("#provider-settings-proxy-env", Input).value = ""
        self._select_preset("openai")
        self._select_proxy_mode(None)
        self._select_background_wake_policy(None)
        self._reset_delete_confirmation()
        if not self.first_run:
            self.query_one("#provider-settings-delete", Button).disabled = True
        self._show_provider_error("")
        name_input.focus()

    def _select_preset(self, preset_name: str, *, update_endpoint: bool = True) -> None:
        preset = next(
            (entry for entry in _PROVIDER_PRESETS if entry.name == preset_name),
            None,
        )
        if preset is None:
            return
        self._clear_model_catalog()
        self._active_preset = preset_name
        for candidate in _PROVIDER_PRESETS:
            button = self.query_one(f"#provider-settings-preset-{candidate.name}", Button)
            button.variant = "primary" if candidate.name == preset_name else "default"
        self.query_one("#provider-settings-protocol-hint", Static).update(
            ui_text(self.language, f"provider_settings.protocol.{preset_name}")
        )
        self.query_one("#provider-settings-model", Input).placeholder = ui_text(
            self.language,
            f"provider_settings.model.{preset_name}",
        )
        if update_endpoint:
            self.query_one("#provider-settings-base-url", Input).value = preset.base_url

    def _select_proxy_mode(self, proxy_mode: str | None) -> None:
        if proxy_mode not in {None, "environment", "direct", "explicit"}:
            return
        self._clear_model_catalog()
        self._active_proxy_mode = proxy_mode
        for candidate in (None, "environment", "direct", "explicit"):
            name = "inherit" if candidate is None else candidate
            button = self.query_one(f"#provider-settings-proxy-{name}", Button)
            button.variant = "primary" if candidate == proxy_mode else "default"
        proxy_env = self.query_one("#provider-settings-proxy-env", Input)
        proxy_env.disabled = proxy_mode != "explicit"
        self.query_one("#provider-settings-proxy-hint", Static).update(
            ui_text(
                self.language,
                (
                    "provider_settings.proxy.hint.inherit"
                    if proxy_mode is None
                    else f"provider_settings.proxy.hint.{proxy_mode}"
                ),
                policy=self._global_proxy_policy_label(),
            )
        )
        self._reset_delete_confirmation()

    def _global_proxy_policy_label(self) -> str:
        return ui_text(
            self.language,
            f"network_settings.policy.{self.provider_settings.proxy_defaults.mode}",
        )

    def _select_background_wake_policy(
        self,
        policy: BackgroundTaskWakePolicy | None,
    ) -> None:
        self._active_background_wake_policy = policy
        for candidate in (None, *BackgroundTaskWakePolicy):
            name = "inherit" if candidate is None else candidate.value
            self.query_one(f"#provider-settings-wake-{name}", Button).variant = (
                "primary" if candidate is policy else "default"
            )
        self.query_one("#provider-settings-background-wake-hint", Static).update(
            ui_text(
                self.language,
                "provider_settings.background_wake.hint",
            )
        )

    def _draft_proxy_policy(self) -> ManagedProxyPolicy:
        if self._active_proxy_mode is None:
            return self.provider_settings.proxy_defaults
        proxy_url_env = (
            self.query_one("#provider-settings-proxy-env", Input).value.strip() or None
            if self._active_proxy_mode == "explicit"
            else None
        )
        return ManagedProxyPolicy(self._active_proxy_mode, proxy_url_env)

    def _context_window_tokens(self) -> int | None:
        value = self.query_one("#provider-settings-context-window", Input).value.strip()
        if not value:
            return None
        try:
            context_window_tokens = int(value)
        except ValueError as error:
            raise ValueError(
                ui_text(self.language, "provider_settings.context_window_invalid")
            ) from error
        if context_window_tokens <= 0:
            raise ValueError(ui_text(self.language, "provider_settings.context_window_invalid"))
        return context_window_tokens

    def _connection_spec(self) -> tuple[ProviderConnectionSpec, HttpClientPolicy]:
        preset = next(entry for entry in _PROVIDER_PRESETS if entry.name == self._active_preset)
        base_url = self.query_one("#provider-settings-base-url", Input).value.strip()
        name = self.query_one("#provider-settings-name", Input).value.strip()
        existing = self.provider_settings.profile(name)
        entered_api_key = self.query_one("#provider-settings-api-key", Input).value.strip()
        api_key = entered_api_key or (existing.api_key if existing is not None else None)
        if api_key is None:
            raise ValueError(ui_text(self.language, "provider_settings.api_key_required"))
        proxy_policy = self._draft_proxy_policy()
        policy = resolve_http_client_policy(
            proxy_mode=proxy_policy.mode,
            proxy_url_env=proxy_policy.proxy_url_env,
            environ=os.environ,
        )
        return (
            ProviderConnectionSpec(
                protocol=preset.protocol,
                dialect=preset.dialect,
                base_url=base_url,
                api_key=api_key,
            ),
            policy,
        )

    async def _test_connection(self) -> None:
        if self.provider_catalog is None:
            return
        button = self.query_one("#provider-settings-test", Button)
        button.disabled = True
        button.label = ui_text(self.language, "provider_settings.connection.testing")
        self._clear_model_catalog()
        self._show_provider_error("")
        self._show_connection_status(
            ui_text(self.language, "provider_settings.connection.testing"),
            kind="normal",
        )
        signature: tuple[str, ...] | None = None
        spec: ProviderConnectionSpec | None = None
        try:
            spec, policy = self._connection_spec()
            signature = self._connection_signature()
            result = await self.provider_catalog.discover_models(spec, http_policy=policy)
            if signature != self._connection_signature():
                self._show_connection_status(
                    ui_text(self.language, "provider_settings.connection.stale"),
                    kind="warning",
                )
                return
            await self._show_model_catalog(result)
        except Exception as error:
            if signature is not None and signature != self._connection_signature():
                self._show_connection_status(
                    ui_text(self.language, "provider_settings.connection.stale"),
                    kind="warning",
                )
            else:
                self._show_connection_status(
                    self._connection_error_message(
                        error,
                        api_key=spec.api_key if spec is not None else None,
                    ),
                    kind="error",
                )
        finally:
            button.disabled = False
            button.label = ui_text(self.language, "provider_settings.connection.test")

    def _connection_signature(self) -> tuple[str, ...]:
        return (
            self._active_preset,
            self._active_proxy_mode or "inherit",
            self.provider_settings.proxy_defaults.mode,
            self.provider_settings.proxy_defaults.proxy_url_env or "",
            self.query_one("#provider-settings-name", Input).value,
            self.query_one("#provider-settings-base-url", Input).value,
            self.query_one("#provider-settings-api-key", Input).value,
            self.query_one("#provider-settings-proxy-env", Input).value,
        )

    async def _show_model_catalog(self, result: ProviderCatalogResult) -> None:
        container = self.query_one("#provider-settings-models", VerticalScroll)
        await container.remove_children()
        self._catalog_model_ids = {
            f"provider-settings-catalog-model-{index}": model
            for index, model in enumerate(result.models)
        }
        if result.models:
            await container.mount(
                *(
                    Button(Text(model), id=button_id)
                    for button_id, model in self._catalog_model_ids.items()
                )
            )
            container.display = True
        else:
            container.display = False
        selected_model = self.query_one("#provider-settings-model", Input).value.strip()
        if not result.models:
            message = ui_text(self.language, "provider_settings.connection.success_empty")
            kind = "success"
        elif selected_model and selected_model in result.models:
            message = ui_text(
                self.language,
                "provider_settings.connection.success_selected",
                count=len(result.models),
                model=selected_model,
            )
            kind = "success"
        elif selected_model:
            message = ui_text(
                self.language,
                "provider_settings.connection.success_missing",
                count=len(result.models),
                model=selected_model,
            )
            kind = "warning"
        else:
            message = ui_text(
                self.language,
                "provider_settings.connection.success",
                count=len(result.models),
            )
            kind = "success"
        if result.truncated:
            message += ui_text(self.language, "provider_settings.connection.truncated")
        self._show_connection_status(message, kind=kind)

    def _connection_error_message(self, error: Exception, *, api_key: str | None = None) -> str:
        if isinstance(error, ProviderCatalogError):
            key = {
                "authentication": "authentication",
                "endpoint": "endpoint",
                "timeout": "timeout",
                "rate_limit": "rate_limit",
                "server": "server",
                "http": "http",
                "proxy": "proxy",
                "network": "network",
                "response_too_large": "response_too_large",
                "invalid_response": "invalid_response",
            }.get(error.kind, "unknown")
            return ui_text(
                self.language,
                f"provider_settings.connection.error.{key}",
                status=error.status_code if error.status_code is not None else "?",
                detail=error.detail or ui_text(self.language, "value.unknown"),
            )
        entered_api_key = self.query_one("#provider-settings-api-key", Input).value.strip()
        return redact_sensitive_text(str(error), explicit_values=(entered_api_key, api_key or ""))

    def _clear_model_catalog(self) -> None:
        self._catalog_model_ids = {}
        if self.is_mounted:
            self.query_one("#provider-settings-models", VerticalScroll).display = False
            self.query_one("#provider-settings-connection-status", Static).update("")

    def _show_connection_status(self, message: str, *, kind: str) -> None:
        color = CONNECTION_STATUS_STYLES.get(kind, TEXT_SECONDARY)
        marker = {
            "success": _SUCCESS_MARK,
            "warning": _WARNING_MARK,
            "error": _ERROR_MARK,
        }.get(kind, "…")
        self.query_one("#provider-settings-connection-status", Static).update(
            Text(f"{marker} {message}", style=color)
        )

    async def _save_provider(self) -> None:
        preset = next(entry for entry in _PROVIDER_PRESETS if entry.name == self._active_preset)
        api_key = self.query_one("#provider-settings-api-key", Input).value.strip() or None
        name = self.query_one("#provider-settings-name", Input).value.strip()
        base_url = self.query_one("#provider-settings-base-url", Input).value.strip()
        try:
            proxy_policy = self._draft_proxy_policy()
            profile = ManagedProviderProfile(
                name=name,
                protocol=preset.protocol,
                dialect=preset.dialect,
                model=self.query_one("#provider-settings-model", Input).value.strip(),
                base_url=base_url,
                context_window_tokens=self._context_window_tokens(),
                proxy_mode=self._active_proxy_mode,
                proxy_url_env=(
                    proxy_policy.proxy_url_env if self._active_proxy_mode is not None else None
                ),
                api_key=api_key,
                background_task_wake_policy=self._active_background_wake_policy,
            )
            existing = self.provider_settings.profile(name)
            if existing is None and api_key is None:
                raise ValueError(ui_text(self.language, "provider_settings.api_key_required"))
            resolve_http_client_policy(
                proxy_mode=proxy_policy.mode,
                proxy_url_env=proxy_policy.proxy_url_env,
                environ=os.environ,
            )
            await self.provider_settings_store.save_profile(profile, make_default=True)
        except Exception as error:
            self._show_provider_error(str(error))
            return
        self.dismiss(ProviderSettingsSubmission(profile.name))

    async def _delete_provider(self) -> None:
        profile_name = self._editing_profile
        if profile_name is None:
            return
        if self._delete_confirmation_for != profile_name:
            self._delete_confirmation_for = profile_name
            button = self.query_one("#provider-settings-delete", Button)
            button.label = ui_text(self.language, "provider_settings.delete_confirm")
            button.variant = "error"
            self._show_provider_error(
                ui_text(
                    self.language,
                    "provider_settings.delete_warning",
                    profile=profile_name,
                )
            )
            return
        try:
            await self.provider_settings_store.delete_profile(profile_name)
        except Exception as error:
            self._show_provider_error(str(error))
            return
        self.dismiss(ProviderSettingsSubmission(profile_name, operation="deleted"))

    def _reset_delete_confirmation(self) -> None:
        self._delete_confirmation_for = None
        if not self.first_run and self.is_mounted:
            button = self.query_one("#provider-settings-delete", Button)
            button.label = ui_text(self.language, "provider_settings.delete")
            button.variant = "default"

    def _show_provider_error(self, message: str) -> None:
        self.query_one("#provider-settings-error", Static).update(
            Text(f"{_ERROR_MARK} {message}", style=ERROR_TEXT_STYLE)
        )

    def action_cancel(self) -> None:
        self.dismiss(None)


class ProviderSetupApp(App[bool]):
    """Focused provider setup used for first run and recoverable startup errors.

    用于首次运行和可恢复启动错误的聚焦 Provider 配置界面."""

    CSS = """
    Screen {
        background: $background;
        color: $text-primary;
    }

    Button {
        background: $surface;
        color: $text-primary;
        border: none;
    }

    Button:hover {
        background: $surface-hover;
    }

    Button:focus {
        background: $surface-selected;
        border-left: tall $border-focus;
        text-style: bold;
    }

    Button:disabled {
        background: $background;
        color: $text-disabled;
        border: none;
    }

    Input {
        background: $surface;
        color: $text-primary;
        border: tall $border;
    }

    Input:focus {
        border: tall $border-focus;
    }
    """

    def __init__(
        self,
        *,
        provider_settings: ManagedProviderSettings,
        provider_settings_store: ProviderSettingsStore,
        provider_catalog: ProviderCatalog | None = None,
        language: UiLanguage = UiLanguage.ENGLISH,
        first_run: bool = True,
        initial_profile: str | None = None,
        initial_error: str | None = None,
    ) -> None:
        super().__init__()
        self.register_theme(TEXTUAL_THEME)
        self.theme = TEXTUAL_THEME.name
        self._provider_settings = provider_settings
        self._provider_settings_store = provider_settings_store
        self._provider_catalog = provider_catalog
        self._language = language
        self._first_run = first_run
        self._initial_profile = initial_profile
        self._initial_error = initial_error

    def on_mount(self) -> None:
        self.push_screen(
            ProviderSettingsScreen(
                language=self._language,
                provider_settings=self._provider_settings,
                provider_settings_store=self._provider_settings_store,
                provider_catalog=self._provider_catalog,
                first_run=self._first_run,
                initial_profile=self._initial_profile,
                initial_error=self._initial_error,
            ),
            self._setup_finished,
        )

    def _setup_finished(
        self,
        result: ProviderSettingsSubmission | None,
    ) -> None:
        self.exit(isinstance(result, ProviderSettingsSubmission))


class ReasoningEffortScreen(ModalScreen[ReasoningEffort | None]):
    """Select application-owned review depth without implying native API support.

    选择应用层拥有的审查深度,不暗示底层 API 原生支持."""

    CSS = """
    ReasoningEffortScreen {
        align: center middle;
        background: $background 85%;
    }

    #effort-dialog {
        width: 90%;
        max-width: 100;
        height: auto;
        max-height: 90%;
        padding: 1 2;
        border: solid $border;
        background: $surface;
    }

    #effort-title {
        text-style: bold;
        color: $text-primary;
        margin-bottom: 1;
    }

    #effort-options {
        height: auto;
        max-height: 20;
    }

    #effort-options Button {
        width: 100%;
        margin-bottom: 1;
        content-align: left middle;
    }

    #effort-help {
        color: $text-muted;
        margin-top: 1;
    }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("ctrl+c", "cancel", "Cancel", show=False),
    ]

    def __init__(
        self,
        selected: ReasoningEffort,
        *,
        language: UiLanguage = UiLanguage.ENGLISH,
    ) -> None:
        super().__init__()
        self.selected = selected
        self.language = language
        self._choice_ids = {
            f"effort-choice-{index}": effort for index, effort in enumerate(ReasoningEffort)
        }

    def _label(self, effort: ReasoningEffort) -> Text:
        color = EFFORT_STYLES[effort.value]
        rendered = Text(f"{effort.glyph}  {effort.value}", style=f"bold {color}")
        rendered.append(
            f"  ·  {ui_text(self.language, f'effort.description.{effort.value}')}",
            style=TEXT_SECONDARY,
        )
        if effort is self.selected:
            rendered.append(
                f"  ({ui_text(self.language, 'marker.current')})",
                style=f"bold {TEXT_PRIMARY}",
            )
        if effort is ReasoningEffort.ULTRACODE:
            rendered.append(
                f"  ({ui_text(self.language, 'effort.workflow_planned')})",
                style=TEXT_MUTED,
            )
        return rendered

    def compose(self) -> ComposeResult:
        buttons = [
            Button(
                self._label(effort),
                id=f"effort-choice-{index}",
                variant="primary" if effort is self.selected else "default",
            )
            for index, effort in enumerate(ReasoningEffort)
        ]
        yield Vertical(
            Label(ui_text(self.language, "effort.title"), id="effort-title"),
            VerticalScroll(*buttons, id="effort-options"),
            Static(ui_text(self.language, "effort.help"), id="effort-help"),
            id="effort-dialog",
        )

    def on_mount(self) -> None:
        index = tuple(ReasoningEffort).index(self.selected)
        self.query_one(f"#effort-choice-{index}", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        effort = self._choice_ids.get(event.button.id or "")
        if effort is not None:
            self.dismiss(effort)

    def action_cancel(self) -> None:
        self.dismiss(None)


class PermissionApprovalScreen(ModalScreen[PermissionApproval]):
    """Fail-closed modal for one bounded permission request.

    用于单个有界权限请求的故障关闭模态框."""

    CSS = """
    PermissionApprovalScreen {
        align: center middle;
        background: $background 85%;
    }

    #approval-dialog {
        width: 80%;
        max-width: 100;
        height: auto;
        max-height: 90%;
        padding: 1 2;
        border: solid $border-focus;
        background: $surface;
    }

    #approval-title {
        text-style: bold;
        color: $text-primary;
        margin-bottom: 1;
    }

    #approval-summary {
        height: auto;
        max-height: 12;
        overflow-y: auto;
        margin: 1 0;
        padding: 1;
        border: round $border;
    }

    #approval-reason {
        color: $text-muted;
        margin-bottom: 1;
    }

    #approval-actions {
        height: auto;
        align-horizontal: right;
    }

    #approval-actions Button {
        margin-left: 1;
    }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "deny", "Deny", show=False),
        Binding("ctrl+c", "deny", "Deny", show=False),
        Binding("d", "deny", "Deny", show=False),
        Binding("a", "allow_once", "Allow once", show=False),
        Binding("s", "allow_session", "Allow for session", show=False),
    ]

    def __init__(
        self,
        request: PermissionRequest,
        *,
        language: UiLanguage = UiLanguage.ENGLISH,
    ) -> None:
        super().__init__()
        self.request = request
        self.language = language

    def compose(self) -> ComposeResult:
        yield Vertical(
            Label(ui_text(self.language, "approval.title"), id="approval-title"),
            Static(
                Text(
                    ui_text(
                        self.language,
                        "approval.tool",
                        tool=self.request.tool_name,
                    )
                )
            ),
            Static(Text(self.request.summary), id="approval-summary"),
            Static(
                Text(
                    ui_text(
                        self.language,
                        "approval.policy",
                        policy=self.request.reason,
                    )
                ),
                id="approval-reason",
            ),
            Horizontal(
                Button(
                    ui_text(self.language, "approval.allow_once"),
                    variant="success",
                    id="approval-allow-once",
                ),
                Button(
                    ui_text(self.language, "approval.allow_session"),
                    variant="primary",
                    id="approval-allow-session",
                    disabled=self.request.scope_key is None,
                    tooltip=(
                        ui_text(self.language, "approval.unscoped")
                        if self.request.scope_key is None
                        else None
                    ),
                ),
                Button(
                    ui_text(self.language, "approval.deny"),
                    variant="error",
                    id="approval-deny",
                ),
                id="approval-actions",
            ),
            id="approval-dialog",
        )

    def on_mount(self) -> None:
        self.query_one("#approval-deny", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        choices = {
            "approval-allow-once": PermissionApproval.allow_once(),
            "approval-allow-session": PermissionApproval.allow_session(),
            "approval-deny": PermissionApproval.deny(),
        }
        approval = choices.get(event.button.id or "")
        if approval is not None:
            self.dismiss(approval)

    def action_allow_once(self) -> None:
        self.dismiss(PermissionApproval.allow_once())

    def action_allow_session(self) -> None:
        if self.request.scope_key is not None:
            self.dismiss(PermissionApproval.allow_session())

    def action_deny(self) -> None:
        self.dismiss(PermissionApproval.deny())


class ProviderSelectionScreen(ModalScreen[str | None]):
    """Select one configured profile without exposing credentials or endpoints.

    选择一个已配置的配置档,不暴露凭据或端点."""

    CSS = """
    ProviderSelectionScreen {
        align: center middle;
        background: $background 85%;
    }

    #provider-dialog {
        width: 90%;
        max-width: 110;
        height: 80%;
        padding: 1 2;
        border: solid $border;
        background: $surface;
    }

    #provider-title {
        text-style: bold;
        color: $text-primary;
        margin-bottom: 1;
    }

    #provider-options {
        height: 1fr;
    }

    #provider-options Button {
        width: 100%;
        margin-bottom: 1;
    }

    #provider-help {
        color: $text-muted;
        margin-top: 1;
    }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("ctrl+c", "cancel", "Cancel", show=False),
    ]

    def __init__(
        self,
        options: tuple[ProviderOption, ...],
        *,
        language: UiLanguage = UiLanguage.ENGLISH,
    ) -> None:
        super().__init__()
        self.options = options
        self.language = language
        self._choice_ids = {
            f"provider-choice-{index}": option.name for index, option in enumerate(options)
        }

    @staticmethod
    def _label(
        option: ProviderOption,
        language: UiLanguage = UiLanguage.ENGLISH,
    ) -> str:
        markers: list[str] = []
        if option.selected:
            markers.append(ui_text(language, "marker.current"))
        if option.default:
            markers.append(ui_text(language, "marker.default"))
        if not option.available:
            markers.append(ui_text(language, "marker.unavailable"))
        elif not option.credential_configured:
            markers.append(ui_text(language, "marker.credential_missing"))
        suffix = f" ({' · '.join(markers)})" if markers else ""
        return f"{option.name} · {option.model} · {option.protocol}{suffix}"

    def compose(self) -> ComposeResult:
        buttons = [
            Button(
                Text(self._label(option, self.language)),
                id=f"provider-choice-{index}",
                variant="primary" if option.selected else "default",
                disabled=not option.selectable,
            )
            for index, option in enumerate(self.options)
        ]
        yield Vertical(
            Label(ui_text(self.language, "provider.title"), id="provider-title"),
            VerticalScroll(*buttons, id="provider-options"),
            Static(
                ui_text(self.language, "provider.help"),
                id="provider-help",
            ),
            id="provider-dialog",
        )

    def on_mount(self) -> None:
        target: Button | None = None
        for index, option in enumerate(self.options):
            button = self.query_one(f"#provider-choice-{index}", Button)
            if option.selected and not button.disabled:
                target = button
                break
            if target is None and not button.disabled:
                target = button
        if target is not None:
            target.focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        profile_name = self._choice_ids.get(event.button.id or "")
        if profile_name is not None:
            self.dismiss(profile_name)

    def action_cancel(self) -> None:
        self.dismiss(None)


class SessionSelectionScreen(ModalScreen[str | None]):
    """Select one recent session already constrained to the active workspace.

    选择一个已限制在当前工作区内的最近会话."""

    CSS = """
    SessionSelectionScreen {
        align: center middle;
        background: $background 85%;
    }

    #session-dialog {
        width: 90%;
        max-width: 115;
        height: 80%;
        padding: 1 2;
        border: solid $border;
        background: $surface;
    }

    #session-title {
        text-style: bold;
        color: $text-primary;
        margin-bottom: 1;
    }

    #session-search {
        margin-bottom: 1;
    }

    #session-options {
        height: 1fr;
    }

    #session-options Button {
        width: 100%;
        margin-bottom: 1;
    }

    #session-help {
        color: $text-muted;
        margin-top: 1;
    }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("ctrl+c", "cancel", "Cancel", show=False),
    ]

    def __init__(
        self,
        options: tuple[SessionOption, ...],
        *,
        query: str | None = None,
        language: UiLanguage = UiLanguage.ENGLISH,
        search_callback: SessionSearchCallback | None = None,
    ) -> None:
        super().__init__()
        self.options = options
        self.search_query = query
        self.language = language
        self._search_callback = search_callback
        self._search_generation = 0
        self._search_ready = False
        self._choice_ids = {
            f"session-choice-{index}": option.session_id for index, option in enumerate(options)
        }

    def _option_buttons(self) -> list[Button]:
        return [
            Button(
                Text(self._label(option, self.language)),
                id=f"session-choice-{index}",
                variant="primary" if option.current else "default",
                disabled=not option.selectable,
                tooltip=option.session_id,
            )
            for index, option in enumerate(self.options)
        ]

    @staticmethod
    def _label(
        option: SessionOption,
        language: UiLanguage = UiLanguage.ENGLISH,
    ) -> str:
        markers: list[str] = []
        if option.current:
            markers.append(ui_text(language, "marker.current"))
        if not option.source_profile_match:
            markers.append(ui_text(language, "session.resume_via", profile=option.resume_profile))
        if option.sandbox_profile is None:
            markers.append(ui_text(language, "session.legacy_sandbox"))
        else:
            markers.append(
                ui_text(
                    language,
                    "session.sandbox",
                    profile=option.sandbox_profile.value,
                )
            )
        if not option.sandbox_profile_match:
            markers.append(ui_text(language, "session.restart_required"))
        if not option.selectable:
            markers.append(ui_text(language, "marker.unavailable"))
        suffix = f" ({' · '.join(markers)})" if markers else ""
        timestamp = option.updated_at.astimezone().strftime("%Y-%m-%d %H:%M")
        short_id = option.session_id if len(option.session_id) <= 12 else option.session_id[:12]
        title = option.title or short_id
        identity = f"{short_id} · " if option.title is not None and option.title != short_id else ""
        snippet = ""
        if option.snippet:
            bounded = " ".join(option.snippet.split())[:120]
            snippet = f" · {bounded}"
        return (
            f"{title} · {identity}{timestamp} · "
            f"{option.source_provider}/{option.source_model}{suffix}{snippet}"
        )

    def compose(self) -> ComposeResult:
        yield Vertical(
            Label(
                Text(
                    ui_text(
                        self.language,
                        "session.search",
                        query=self.search_query,
                    )
                    if self.search_query is not None
                    else ui_text(self.language, "session.title")
                ),
                id="session-title",
            ),
            Input(
                value=self.search_query or "",
                placeholder=ui_text(self.language, "session.search_placeholder"),
                id="session-search",
            ),
            VerticalScroll(*self._option_buttons(), id="session-options"),
            Static(
                ui_text(self.language, "session.help"),
                id="session-help",
            ),
            id="session-dialog",
        )

    def on_mount(self) -> None:
        if self._search_callback is not None:
            self._search_ready = True
            self.query_one("#session-search", Input).focus()
            return
        target: Button | None = None
        for index, option in enumerate(self.options):
            button = self.query_one(f"#session-choice-{index}", Button)
            if option.current and not button.disabled:
                target = button
                break
            if target is None and not button.disabled:
                target = button
        if target is not None:
            target.focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if (
            event.input.id != "session-search"
            or self._search_callback is None
            or not self._search_ready
        ):
            return
        self._search_generation += 1
        generation = self._search_generation
        self.run_worker(
            self._refresh_search_results(event.value, generation),
            name="session-search",
            group="session-search",
            exclusive=True,
            exit_on_error=False,
        )

    async def _refresh_search_results(self, value: str, generation: int) -> None:
        await asyncio.sleep(0.2)
        callback = self._search_callback
        if callback is None:
            return
        try:
            options = await callback(value.strip() or None)
        except asyncio.CancelledError:
            raise
        except Exception:
            return
        if generation != self._search_generation:
            return
        self.options = options
        self._choice_ids = {
            f"session-choice-{index}": option.session_id for index, option in enumerate(options)
        }
        title = self.query_one("#session-title", Label)
        title.update(
            Text(
                ui_text(
                    self.language,
                    "session.search",
                    query=value.strip(),
                )
                if value.strip()
                else ui_text(self.language, "session.title")
            )
        )
        options_widget = self.query_one("#session-options", VerticalScroll)
        await options_widget.remove_children()
        await options_widget.mount(*self._option_buttons())
        target: Button | None = None
        for index in range(len(options)):
            button = self.query_one(f"#session-choice-{index}", Button)
            if not button.disabled:
                target = button
                if options[index].current:
                    break
        if target is not None:
            target.focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        session_id = self._choice_ids.get(event.button.id or "")
        if session_id is not None:
            self.dismiss(session_id)

    def action_cancel(self) -> None:
        self.dismiss(None)


class NeuroCodeApp(App[None]):
    """Minimal Textual interface over the normalized agent event stream.

    建立在规范化 Agent 事件流之上的最小 Textual 界面."""

    TITLE = "Neuro Code"
    SUB_TITLE = "Terminal coding agent"
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    Screen {
        layout: vertical;
        width: 100%;
        height: 100%;
        background: $background;
        color: $text-primary;
    }

    Button {
        background: $surface;
        color: $text-primary;
        border: none;
    }

    Button:hover {
        background: $surface-hover;
    }

    Button:focus {
        background: $surface-selected;
        color: $text-primary;
        border-left: tall $border-focus;
        text-style: bold;
    }

    Button:disabled {
        background: $background;
        color: $text-disabled;
        border: none;
    }

    Input {
        background: $surface;
        color: $text-primary;
        border: tall $border;
    }

    Input:focus {
        border: tall $border-focus;
    }

    #header {
        height: 3;
        padding: 1 2 0 2;
        background: $background;
    }

    #brand {
        width: auto;
        height: 1;
    }

    #header-space {
        width: 1fr;
    }

    #clock {
        width: auto;
        height: 1;
        color: $text-muted;
        text-align: right;
    }

    #transcript {
        width: 100%;
        height: 1fr;
        padding: 1 2;
        background: $background;
        color: $text-body;
    }

    .conversation-message {
        width: 100%;
        height: auto;
        min-height: 1;
        margin-bottom: 1;
        padding: 0;
        color: $text-body;
    }

    .message-user {
        margin-left: 2;
        padding: 0 1;
        background: $surface;
        color: $text-primary;
        border-left: solid $border;
        text-style: bold;
    }

    .message-assistant {
        margin-right: 2;
        padding: 0 1;
        background: $background;
        color: $text-body;
    }

    .message-pending {
        color: $text-secondary;
        text-style: italic;
    }

    .message-system {
        padding: 0 1;
        color: $text-emphasis;
    }

    .message-tool {
        padding: 0 1;
        color: $text-body;
    }

    .message-tool.tool-interactive:hover,
    .message-tool.tool-interactive:focus {
        background: $surface-hover;
        border-left: solid $border-focus;
    }

    .message-status {
        padding: 0 1;
        color: $text-secondary;
    }

    .message-recoverable {
        padding: 0 1;
        color: $text-emphasis;
        border-left: solid $border;
    }

    .message-error {
        padding: 0 1;
        color: $text-primary;
        border-left: tall $border-focus;
        text-style: bold;
    }

    #composer {
        height: auto;
        padding: 0 2 1 2;
        background: $background;
    }

    #runtime-bar {
        width: 100%;
        height: 2;
        padding: 0 1;
        background: $background;
        border-top: solid $border-dim;
        color: $text-secondary;
        align-vertical: middle;
    }

    #runtime-model {
        width: 1fr;
        height: 1;
        overflow: hidden hidden;
    }

    #runtime-workspace {
        width: 1fr;
        height: 1;
        padding-left: 2;
        text-align: right;
        overflow: hidden hidden;
    }

    #runtime-context {
        width: auto;
        max-width: 20;
        height: 1;
        padding-left: 2;
        overflow: hidden hidden;
    }

    #runtime-effort {
        width: auto;
        max-width: 34;
        height: 1;
        padding-left: 2;
        overflow: hidden hidden;
    }

    #runtime-mode {
        width: auto;
        max-width: 30;
        height: 1;
        padding-left: 2;
        overflow: hidden hidden;
    }

    #prompt-row {
        height: 3;
        padding: 0 1;
        background: $surface;
        border-left: tall $border;
        align-vertical: middle;
    }

    #prompt-mark {
        width: 3;
        height: 1;
        color: $text-primary;
        text-style: bold;
        content-align: center middle;
    }

    #prompt {
        width: 1fr;
        height: 1;
        padding: 0;
        margin: 0;
        border: none;
        background: $surface;
        color: $text-primary;
    }

    #prompt > .input--placeholder {
        color: $text-disabled;
    }

    #prompt-row:focus-within {
        border-left: tall $border-focus;
    }

    #command-hints {
        display: none;
        width: 100%;
        height: auto;
        max-height: 3;
        padding: 0 1;
        background: $background;
        color: $text-secondary;
        overflow: hidden hidden;
    }

    #shortcut-bar {
        height: 2;
        padding: 0 1;
        background: $background;
        color: $text-muted;
    }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+c", "cancel_turn", "Cancel", priority=True, show=False),
        Binding("ctrl+shift+c", "copy_prompt", "Copy", priority=True, show=False),
        Binding("ctrl+p", "select_provider", "Provider", priority=True, show=False),
        Binding("ctrl+r", "select_session", "Sessions", priority=True, show=False),
        Binding("ctrl+e", "select_reasoning_effort", "Effort", priority=True, show=False),
        Binding("ctrl+comma", "open_settings", "Settings", priority=True, show=False),
        Binding("ctrl+q", "quit", "Quit", show=False),
        Binding("ctrl+l", "clear_transcript", "Clear", show=False),
        Binding(
            "shift+tab",
            "cycle_interaction_mode",
            "Mode",
            priority=True,
            show=False,
        ),
        Binding("tab", "complete_slash_command", "Complete", priority=True, show=False),
    ]

    def __init__(
        self,
        runner: ConversationRunner,
        *,
        turn_service: SessionTurnService | None = None,
        approval_controller: ApprovalController | None = None,
        provider_controller: ProviderController | None = None,
        reasoning_controller: ReasoningController | None = None,
        interaction_mode_controller: InteractionModeController | None = None,
        session_controller: SessionController | None = None,
        session_selection_service: SessionSelectionService | None = None,
        task_controller: TaskController | None = None,
        session_task_controller: SessionTaskController | None = None,
        plan_controller: PlanController | None = None,
        plan_execution_service: PlanExecutionService | None = None,
        plan_scheduling_service: PlanSchedulingService | None = None,
        queued_plan_execution_service: QueuedPlanExecutionService | None = None,
        ui_preferences: UiPreferencesStore | None = None,
        provider_settings_store: ProviderSettingsStore | None = None,
        provider_catalog: ProviderCatalog | None = None,
        managed_provider_settings: ManagedProviderSettings | None = None,
        language: UiLanguage = UiLanguage.ENGLISH,
        initial_items: Sequence[SessionItem] = (),
        execution_record: SessionExecutionRecord | None = None,
        tool_output_artifact_service: SessionToolOutputArtifactApplicationService | None = None,
        read_only_subagent_service: ReadOnlySubagentApplicationService | None = None,
        subagent_relationship_query: SubagentRelationshipQueryController | None = None,
        subagent_relationship_lifecycle: SubagentRelationshipLifecycleController | None = None,
        provider_name: str,
        model_name: str,
        cwd: Path,
        reasoning_effort: ReasoningEffort = ReasoningEffort.HIGH,
        interaction_mode: InteractionMode = InteractionMode.NORMAL,
        context_window_tokens: int | None = None,
        background_task_wake_policy: BackgroundTaskWakePolicy | None = None,
        background_wake_limits: BackgroundWakeLimits = _DEFAULT_BACKGROUND_WAKE_LIMITS,
    ) -> None:
        if context_window_tokens is not None and context_window_tokens <= 0:
            raise ValueError("context window tokens must be positive")
        super().__init__()
        self.register_theme(TEXTUAL_THEME)
        self.theme = TEXTUAL_THEME.name
        self._runner = runner
        self._turn_service = turn_service
        self._approval_controller = approval_controller
        self._provider_controller = provider_controller
        if context_window_tokens is None and provider_controller is not None:
            selected_profile = provider_controller.selected_profile
            selected_option = next(
                (
                    option
                    for option in provider_controller.profiles
                    if option.name == selected_profile
                ),
                None,
            )
            if selected_option is not None:
                context_window_tokens = selected_option.context_window_tokens
        if context_window_tokens is not None and context_window_tokens <= 0:
            raise ValueError("context window tokens must be positive")
        if reasoning_controller is None and all(
            hasattr(provider_controller, name)
            for name in (
                "reasoning_effort",
                "effective_reasoning_effort",
                "set_reasoning_effort",
            )
        ):
            reasoning_controller = provider_controller  # type: ignore[assignment]
        self._reasoning_controller = reasoning_controller
        if interaction_mode_controller is None and all(
            hasattr(provider_controller, name)
            for name in (
                "interaction_mode",
                "auto_mode_unrestricted",
                "set_interaction_mode",
            )
        ):
            interaction_mode_controller = provider_controller  # type: ignore[assignment]
        self._interaction_mode_controller = interaction_mode_controller
        self._session_controller = session_controller
        self._session_selection_service = session_selection_service
        self._task_controller = task_controller
        self._session_task_controller = session_task_controller
        self._plan_controller = plan_controller
        self._plan_execution_service = plan_execution_service
        self._plan_scheduling_service = plan_scheduling_service
        self._queued_plan_execution_service = queued_plan_execution_service
        self._ui_preferences = ui_preferences
        self._provider_settings_store = provider_settings_store
        self._provider_catalog = provider_catalog
        self._managed_provider_settings = managed_provider_settings
        if background_task_wake_policy is not None and not isinstance(
            background_task_wake_policy, BackgroundTaskWakePolicy
        ):
            raise TypeError("background_task_wake_policy must be a BackgroundTaskWakePolicy")
        if not isinstance(background_wake_limits, BackgroundWakeLimits):
            raise TypeError("background_wake_limits must be a BackgroundWakeLimits")
        selected_provider = (
            provider_controller.selected_profile
            if provider_controller is not None
            else provider_name
        )
        self._background_task_wake_policy_override = background_task_wake_policy
        self._background_task_wake_policy = (
            background_task_wake_policy
            if background_task_wake_policy is not None
            else managed_provider_settings.effective_background_task_wake_policy(selected_provider)
            if managed_provider_settings is not None
            else BackgroundTaskWakePolicy.DISABLED
        )
        self._background_wake_limits = background_wake_limits
        self._language = language
        self._initial_items = tuple(initial_items)
        self._tool_output_artifact_service = tool_output_artifact_service
        self._read_only_subagent_service = read_only_subagent_service
        self._subagent_relationship_query = subagent_relationship_query
        self._subagent_relationship_lifecycle = subagent_relationship_lifecycle
        runner_record = getattr(runner, "execution_record", None)
        self._execution_record = (
            execution_record
            if execution_record is not None
            else runner_record
            if isinstance(runner_record, SessionExecutionRecord)
            else None
        )
        self._provider_name = provider_name
        self._model_name = model_name
        self._reasoning_effort = (
            reasoning_controller.reasoning_effort
            if reasoning_controller is not None
            else reasoning_effort
        )
        self._effective_reasoning_effort = (
            reasoning_controller.effective_reasoning_effort
            if reasoning_controller is not None
            else self._reasoning_effort.effective
        )
        self._interaction_mode = (
            interaction_mode_controller.interaction_mode
            if interaction_mode_controller is not None
            else interaction_mode
        )
        self._auto_mode_unrestricted = (
            interaction_mode_controller.auto_mode_unrestricted
            if interaction_mode_controller is not None
            else False
        )
        self._cwd = cwd
        self._context_window_tokens = context_window_tokens
        self._context_used_tokens = estimate_context_tokens(self._initial_items)
        self._context_usage_estimated = True
        self._plan = plan_controller.plan if plan_controller is not None else None
        self._plan_comments: tuple[PlanComment, ...] = ()
        self._plan_entry_index: int | None = None
        self._entries: list[TranscriptEntry] = []
        self._entry_widgets: list[ConversationMessage] = []
        self._tool_feedback_by_call: dict[tuple[bool, str], ToolFeedbackState] = {}
        self._tool_feedback_by_entry: dict[int, ToolFeedbackState] = {}
        self._assistant_parts: list[str] = []
        self._first_token_seen = False
        self._queued_interjections: deque[str] = deque()
        self._active_prompt: str | None = None
        self._active_prompt_entry_index: int | None = None
        self._turn_pristine_rewound = False
        self._pending_assistant: ConversationMessage | None = None
        self._reasoning_announced = False
        self._turn_completion: tuple[str, int] | None = None
        self._terminal_execution_status: str | None = None
        self._terminal_execution_recoverable = False
        self._finalizing = False
        self._turn_usage_reported = False
        self._turn_worker: Worker[None] | None = None
        self._model_loading = False
        self._loading_animation = CollapsingPulseAnimation()
        self._loading_animation_elapsed = 0.0
        self._announced_terminal_tasks: set[str] = set()
        self._pending_auto_wake_tasks: set[str] = set()
        self._background_wake_state = BackgroundWakeState()
        self._background_wake_state_loaded = self._task_controller is None
        self._background_wake_active = False
        self._background_wake_task_ids: tuple[str, ...] = ()
        self._task_polling = False

    @property
    def entries(self) -> tuple[TranscriptEntry, ...]:
        return tuple(self._entries)

    def compose(self) -> ComposeResult:
        with Horizontal(id="header"):
            yield Static(id="brand")
            yield Static(id="header-space")
            yield Static(id="clock")
        yield VerticalScroll(id="transcript")
        with Vertical(id="composer"):
            yield Horizontal(
                Static(id="runtime-model"),
                Static(id="runtime-workspace"),
                Static(id="runtime-context"),
                Static(id="runtime-effort"),
                Static(id="runtime-mode"),
                id="runtime-bar",
            )
            with Horizontal(id="prompt-row"):
                yield Static(_PROMPT_MARK, id="prompt-mark")
                yield PromptInput(
                    placeholder=ui_text(self._language, "prompt.placeholder"),
                    suggester=SlashCommandSuggester(self._slash_completions),
                    id="prompt",
                )
            yield Static(id="command-hints")
            yield Static(ui_text(self._language, "shortcuts"), id="shortcut-bar")

    def on_mount(self) -> None:
        self.console.push_theme(MARKDOWN_THEME)
        self._refresh_header()
        self.set_interval(1.0, self._update_clock)
        self._apply_language_to_chrome()
        if self._approval_controller is not None:
            self._approval_controller.set_handler(self._request_approval)
        if self._runner.session_id is not None:
            self._replace_transcript(self._initial_items)
            self._write_ui_entry(
                "system",
                "startup.resumed",
                session_id=self._runner.session_id or ui_text(self._language, "value.unknown"),
                provider=self._provider_name,
                model=self._model_name,
                cwd=self._cwd,
            )
            self._write_recoverable_resume_notice(self._execution_record)
        else:
            self._write_ui_entry(
                "system",
                "startup.ready",
                provider=self._provider_name,
                model=self._model_name,
                cwd=self._cwd,
            )
        if self._task_controller is not None:
            self.set_interval(_TASK_POLL_SECONDS, self._poll_background_tasks)
        self.set_interval(
            _LOADING_ANIMATION_TICK_SECONDS,
            self._advance_model_loading_animation,
        )
        self.set_interval(_TOOL_ELAPSED_UPDATE_SECONDS, self._refresh_running_tool_elapsed)
        if not self.is_headless and not self.is_inline and not self.is_web:
            self.set_interval(_TERMINAL_SIZE_POLL_SECONDS, self._synchronize_terminal_size)
        self.query_one("#prompt", Input).focus()

    def _synchronize_terminal_size(self) -> None:
        """Recover when a terminal drops its normal resize notification.

        在终端丢失常规尺寸变化通知时恢复."""

        terminal_size = _read_terminal_size()
        if terminal_size is None or terminal_size == self.screen.size:
            return
        self.post_message(events.Resize(terminal_size, terminal_size, terminal_size))

    def on_unmount(self) -> None:
        self._model_loading = False
        if self._approval_controller is not None:
            self._approval_controller.set_handler(None)
        self.console.pop_theme()

    def _refresh_header(self) -> None:
        brand = Text()
        brand.append("NEURO", style=f"bold {BRAND_TEXT}")
        brand.append(" / CODE", style=TEXT_MUTED)
        self.query_one("#brand", Static).update(brand)
        self._update_clock()

    def _update_clock(self) -> None:
        current_time = datetime.now(tz=UTC).astimezone()
        clock = next(iter(self.query("#clock")), None)
        if isinstance(clock, Static):
            clock.update(current_time.strftime("%H:%M"))

    async def _request_approval(self, request: PermissionRequest) -> PermissionApproval:
        return await self.push_screen_wait(
            PermissionApprovalScreen(request, language=self._language)
        )

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        prompt = event.value.strip()
        event.input.value = ""
        if not prompt:
            return
        if prompt.startswith("/"):
            await self._dispatch_slash_command(prompt)
            return
        if self._turn_worker is not None and self._turn_worker.is_running:
            if not self._first_token_seen and self._pending_assistant is not None:
                if not self._queue_interjection(prompt):
                    event.input.value = prompt
                    event.input.cursor_position = len(prompt)
            else:
                self._write_ui_entry("error", "turn.running")
            return

        self._submit_prompt(prompt)

    def _submit_prompt(self, prompt: str) -> None:
        if self._turn_worker is not None and self._turn_worker.is_running:
            self._write_ui_entry("error", "turn.running")
            return
        self._active_prompt = prompt
        self._active_prompt_entry_index = len(self._entries)
        self._turn_pristine_rewound = False
        self._write_entry("user", prompt)
        self._context_used_tokens += 4 + estimate_text_tokens(prompt)
        self._context_usage_estimated = True
        self._refresh_runtime_bar()
        self._assistant_parts.clear()
        self._first_token_seen = False
        self._reasoning_announced = False
        self._turn_completion = None
        self._terminal_execution_status = None
        self._terminal_execution_recoverable = False
        self._finalizing = False
        self._turn_usage_reported = False
        self._begin_pending_assistant()
        self._turn_worker = self.run_worker(
            self._run_prompt(prompt),
            name="agent-turn",
            group="agent",
            exclusive=True,
            exit_on_error=False,
        )

    def _queue_interjection(self, prompt: str) -> bool:
        if len(self._queued_interjections) >= _MAX_QUEUED_INTERJECTIONS:
            self._write_ui_entry("error", "turn.interjection_limit")
            return False
        self._queued_interjections.append(prompt)
        self._write_ui_entry("status", "turn.interjection_queued")
        return True

    def _start_next_interjection(self) -> None:
        if (
            self._turn_worker is not None and self._turn_worker.is_running
        ) or not self._queued_interjections:
            return
        self._submit_prompt(self._queued_interjections.popleft())

    def _restore_queued_interjections(self) -> None:
        """Return every unsent interjection to the draft without auto-submitting it.

        将所有未发送的插话放回草稿,不自动提交."""

        if not self._queued_interjections:
            return
        queued = tuple(self._queued_interjections)
        self._queued_interjections.clear()
        prompt = self.query_one("#prompt", Input)
        prompt.value = "\n\n".join((*queued, prompt.value)) if prompt.value else "\n\n".join(queued)
        prompt.cursor_position = len(prompt.value)
        self._write_ui_entry("status", "turn.interjections_restored", count=len(queued))

    async def _restore_pristine_prompt(self) -> None:
        prompt_text = self._active_prompt
        if not prompt_text:
            return
        entry_index = self._active_prompt_entry_index
        prompt = self.query_one("#prompt", Input)
        if not prompt.value:
            if entry_index is not None and 0 <= entry_index < len(self._entries):
                entry = self._entries[entry_index]
                if entry.category == "user" and entry.text == prompt_text:
                    await self._remove_transcript_entry(entry_index)
            prompt.value = prompt_text
            prompt.cursor_position = len(prompt_text)
            self._write_ui_entry("status", "turn.draft_restored")
            return
        self._write_ui_entry("status", "turn.draft_preserved")

    async def _remove_transcript_entry(self, index: int) -> None:
        if index < 0 or index >= len(self._entries):
            return
        widget = self._entry_widgets.pop(index)
        self._entries.pop(index)
        removed_state = self._tool_feedback_by_entry.pop(index, None)
        if removed_state is not None:
            self._tool_feedback_by_call.pop(
                (removed_state.hosted, removed_state.call_id),
                None,
            )
        if self._plan_entry_index == index:
            self._plan_entry_index = None
        elif self._plan_entry_index is not None and self._plan_entry_index > index:
            self._plan_entry_index -= 1
        shifted: dict[int, ToolFeedbackState] = {}
        for entry_index, state in self._tool_feedback_by_entry.items():
            if entry_index > index:
                state.entry_index -= 1
                shifted[entry_index - 1] = state
            else:
                shifted[entry_index] = state
        self._tool_feedback_by_entry = shifted
        if widget.parent is not None:
            await widget.remove()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "prompt" and event.input.screen is self.screen:
            self._refresh_command_hints(event.value)

    def on_tool_feedback_message_toggle_requested(
        self,
        event: ToolFeedbackMessage.ToggleRequested,
    ) -> None:
        state = self._tool_feedback_by_entry.get(event.card.entry_index)
        if state is None or not self._tool_details_available(
            state,
            self._tool_change_report(state),
        ):
            return
        state.expanded = not state.expanded
        self._refresh_tool_feedback(state)
        if state.expanded:
            self._request_tool_artifact(state)
        event.card.focus()

    def _request_tool_artifact(self, state: ToolFeedbackState) -> None:
        if (
            state.artifact_id is None
            or self._tool_output_artifact_service is None
            or self._runner.session_id is None
            or state.artifact_loading
            or state.artifact_content is not None
        ):
            return
        state.artifact_loading = True
        self._refresh_tool_feedback(state)
        self.run_worker(
            self._load_tool_artifact(state),
            name=f"tool-output-artifact-{state.entry_index}",
            group="tool-output-artifact",
            exclusive=False,
            exit_on_error=False,
        )

    async def _load_tool_artifact(self, state: ToolFeedbackState) -> None:
        service = self._tool_output_artifact_service
        session_id = self._runner.session_id
        artifact_id = state.artifact_id
        if service is None or session_id is None or artifact_id is None:
            state.artifact_loading = False
            return
        try:
            result = await service.read(
                ReadSessionToolOutputArtifactRequest(
                    session_id=session_id,
                    artifact_id=artifact_id,
                )
            )
            if (
                self._runner.session_id != session_id
                or self._tool_feedback_by_entry.get(state.entry_index) is not state
            ):
                return
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.debug("tool output artifact is unavailable", exc_info=True)
            state.artifact_unavailable = True
        else:
            state.artifact_content = result.content
            state.artifact_read_truncated = result.read_truncated
            state.artifact_unavailable = False
        finally:
            state.artifact_loading = False
            if self._tool_feedback_by_entry.get(state.entry_index) is state:
                self._refresh_tool_feedback(state)

    def action_complete_slash_command(self) -> None:
        if isinstance(self.screen, ModalScreen):
            self.screen.focus_next()
            return
        prompt = self.query_one("#prompt", Input)
        if not prompt.has_focus:
            self.screen.focus_next()
            return
        if not prompt.value.startswith("/"):
            self.screen.focus_next()
            return
        completions = self._slash_completions(prompt.value)
        if not completions:
            return
        completed = completions[0].value
        if completed == prompt.value:
            return
        prompt.value = completed
        prompt.cursor_position = len(completed)

    async def _run_prompt(self, prompt: str) -> None:
        turn_service = self._turn_service
        if turn_service is not None:
            request = RunTurnRequest(
                prompt,
                cancellation_policy=TurnCancellationPolicy.REWIND_PRISTINE,
                expected_session_id=self._runner.session_id,
            )
            await self._run_agent_turn(
                lambda: turn_service.run_turn(request, sink=self._handle_event)
            )
            return
        await self._run_agent_turn(
            lambda: self._runner.run(
                prompt,
                sink=self._handle_event,
                cancellation_policy=TurnCancellationPolicy.REWIND_PRISTINE,
            )
        )

    async def _run_background_wake(self) -> None:
        await self._run_agent_turn(
            lambda: self._runner.run_background_wake(sink=self._handle_event)
        )

    async def _run_plan_execution(self) -> None:
        controller = self._plan_controller
        if controller is None:
            self._write_ui_entry("error", "plan.execution_unavailable")
            return
        service = self._plan_execution_service
        if service is not None:
            await self._run_agent_turn(
                lambda: service.execute_plan(
                    ExecutePlanRequest(),
                    sink=self._handle_event,
                )
            )
            return
        await self._run_agent_turn(lambda: controller.execute_plan(sink=self._handle_event))

    async def _run_queued_plan(self, task_id: str) -> None:
        controller = self._plan_controller
        if controller is None:
            self._write_ui_entry("error", "plan.execution_unavailable")
            return
        service = self._queued_plan_execution_service
        if service is not None:
            await self._run_agent_turn(
                lambda: service.run_session_task(
                    RunSessionTaskRequest(task_id),
                    sink=self._handle_event,
                )
            )
            return
        await self._run_agent_turn(
            lambda: controller.run_session_task(task_id, sink=self._handle_event)
        )

    async def _run_agent_turn(
        self,
        run: Callable[[], Awaitable[AgentRunResult]],
    ) -> None:
        prompt_input = self.query_one("#prompt", Input)
        completed = False
        try:
            result = await run()
            if self._background_wake_active:
                await self._complete_background_wake()
            completed = True
            response = result.response or ui_text(self._language, "turn.no_response")
            if not self._turn_usage_reported:
                self._context_used_tokens = (
                    estimate_context_tokens(result.items)
                    if result.items
                    else self._context_used_tokens + 4 + estimate_text_tokens(response)
                )
                self._context_usage_estimated = True
                self._refresh_runtime_bar()
            self._finish_pending_assistant(response)
            if self._terminal_execution_recoverable and self._terminal_execution_status is not None:
                self._write_ui_entry(
                    "recoverable",
                    f"turn.{self._terminal_execution_status}_recoverable",
                )
            elif self._turn_completion is not None:
                duration, steps = self._turn_completion
                self._write_ui_entry(
                    "status",
                    "turn.completed",
                    duration=duration,
                    steps=steps,
                )
        except asyncio.CancelledError:
            await self._discard_pending_assistant()
            if self._turn_pristine_rewound:
                await self._restore_pristine_prompt()
            self._restore_queued_interjections()
            self._write_ui_entry("status", "turn.cancelled")
            raise
        except Exception as error:
            await self._discard_pending_assistant()
            self._restore_queued_interjections()
            self._write_entry("error", f"{type(error).__name__}: {error}")
        finally:
            if self._background_wake_active:
                self._background_wake_state = self._background_wake_state.abandon_wake(
                    failed_at=datetime.now(UTC)
                )
                self._background_wake_active = False
                self._background_wake_task_ids = ()
                await self._persist_background_wake_state()
            self._stop_model_loading()
            prompt_input.focus()
            if completed and self._queued_interjections:
                self.call_after_refresh(self._start_next_interjection)
            if completed or self._turn_pristine_rewound or self._active_prompt is not None:
                self._active_prompt = None
                self._active_prompt_entry_index = None

    async def _handle_event(self, event: AgentEvent) -> None:
        data = event.data
        if event.kind is AgentEventKind.TEXT_DELTA:
            text = data.get("text")
            if isinstance(text, str):
                self._finalizing = False
                if text:
                    self._first_token_seen = True
                self._assistant_parts.append(text)
                self._update_pending_assistant("".join(self._assistant_parts))
        elif event.kind is AgentEventKind.FINALIZING_STARTED:
            self._finalizing = True
            pending = self._pending_assistant
            if pending is not None and not self._assistant_parts:
                pending.update(self._render_model_loading())
        elif event.kind is AgentEventKind.REASONING_DELTA:
            text = data.get("text")
            if isinstance(text, str) and text:
                self._first_token_seen = True
            if not self._reasoning_announced:
                self._reasoning_announced = True
                self._write_ui_entry("status", "turn.reasoning")
        elif event.kind is AgentEventKind.MODEL_THINKING_COMPLETED:
            self._write_ui_entry(
                "status",
                "turn.thinking_completed",
                duration=self._event_duration(data),
                step=self._positive_int(data.get("step"), fallback=1),
            )
        elif event.kind is AgentEventKind.CONTEXT_USAGE_UPDATED:
            used_tokens = data.get("used_tokens")
            if isinstance(used_tokens, int) and not isinstance(used_tokens, bool):
                self._context_used_tokens = max(0, used_tokens)
                self._context_usage_estimated = data.get("estimated") is not False
                self._turn_usage_reported = not self._context_usage_estimated
                self._refresh_runtime_bar()
        elif event.kind is AgentEventKind.BACKGROUND_TASK_COMPLETION_REMINDER:
            raw_task_ids = data.get("task_ids")
            if (
                self._background_wake_active
                and isinstance(raw_task_ids, Sequence)
                and not isinstance(raw_task_ids, str | bytes)
            ):
                task_ids = tuple(task_id for task_id in raw_task_ids if isinstance(task_id, str))
                self._background_wake_task_ids = task_ids
        elif event.kind is AgentEventKind.PROVIDER_ATTEMPT_FAILED:
            provider = self._field(data, "provider")
            message = self._field(data, "message")
            self._write_ui_entry(
                "error",
                "provider.failed",
                provider=provider,
                message=message,
            )
        elif event.kind is AgentEventKind.PROVIDER_SELECTED:
            provider = self._field(data, "provider")
            model = self._field(data, "model")
            self._provider_name = provider
            self._model_name = model
            context_window_tokens = data.get("context_window_tokens")
            self._context_window_tokens = (
                context_window_tokens
                if isinstance(context_window_tokens, int)
                and not isinstance(context_window_tokens, bool)
                and context_window_tokens > 0
                else None
            )
            self._refresh_runtime_bar()
            key = (
                "provider.fallback_selected"
                if data.get("failover") is True
                else "provider.selected"
            )
            self._write_ui_entry("status", key, provider=provider, model=model)
        elif event.kind in {
            AgentEventKind.BACKEND_TOOL_STARTED,
            AgentEventKind.BACKEND_TOOL_COMPLETED,
            AgentEventKind.TOOL_REQUESTED,
            AgentEventKind.TOOL_PERMISSION,
            AgentEventKind.TOOL_APPROVAL_REQUESTED,
            AgentEventKind.TOOL_APPROVAL_RESOLVED,
            AgentEventKind.TOOL_STARTED,
            AgentEventKind.TOOL_COMPLETED,
            AgentEventKind.TOOL_FAILED,
        }:
            self._handle_tool_feedback_event(event)
        elif event.kind is AgentEventKind.PLAN_UPDATED:
            try:
                self._plan = SessionPlan.from_dict(data)
            except ValueError:
                return
            self._plan_comments = ()
            self._upsert_plan_entry(self._plan)
        elif event.kind is AgentEventKind.PLAN_EXECUTION_REQUESTED:
            self._write_ui_entry("status", "plan.execution_requested")
        elif event.kind is AgentEventKind.TURN_COMPLETED:
            self._finalizing = False
            self._turn_completion = (
                self._event_duration(data),
                self._positive_int(data.get("step"), fallback=1),
            )
            execution_status = recoverable_terminal_status(data)
            if execution_status is not None:
                self._terminal_execution_status = execution_status.value
                self._terminal_execution_recoverable = True
            else:
                self._terminal_execution_status = None
                self._terminal_execution_recoverable = False
        elif event.kind is AgentEventKind.TURN_FAILED:
            self._turn_pristine_rewound = data.get("pristine_rewound") is True

    def _handle_tool_feedback_event(self, event: AgentEvent) -> None:
        hosted = event.kind in {
            AgentEventKind.BACKEND_TOOL_STARTED,
            AgentEventKind.BACKEND_TOOL_COMPLETED,
        }
        starts_card = event.kind in {
            AgentEventKind.BACKEND_TOOL_STARTED,
            AgentEventKind.TOOL_REQUESTED,
        }
        state = (
            self._start_tool_feedback(event, hosted=hosted)
            if starts_card
            else self._find_or_start_tool_feedback(event, hosted=hosted)
        )
        data = event.data
        if event.kind is AgentEventKind.BACKEND_TOOL_STARTED:
            state.phase = "running"
            if state.started_at is None:
                state.started_at = monotonic()
        elif event.kind is AgentEventKind.BACKEND_TOOL_COMPLETED:
            state.phase = "completed"
            state.duration = self._event_duration(data)
            state.started_at = None
        elif event.kind is AgentEventKind.TOOL_PERMISSION:
            state.permission_effect = self._optional_text(data.get("effect"))
            state.permission_reason = self._optional_text(data.get("reason"))
            if state.permission_effect == "deny":
                state.phase = "permission_denied"
            elif state.permission_effect == "ask":
                state.phase = "approval_required"
            else:
                state.phase = "permitted"
        elif event.kind is AgentEventKind.TOOL_APPROVAL_REQUESTED:
            state.phase = "awaiting_approval"
        elif event.kind is AgentEventKind.TOOL_APPROVAL_RESOLVED:
            state.approval_effect = self._optional_text(data.get("effect"))
            state.approval_outcome = self._optional_text(data.get("outcome"))
            state.approval_reason = self._optional_text(data.get("reason"))
            state.phase = (
                "approval_denied" if state.approval_effect == "deny" else "approval_resolved"
            )
        elif event.kind is AgentEventKind.TOOL_STARTED:
            state.phase = "running"
            if state.started_at is None:
                state.started_at = monotonic()
        elif event.kind in {AgentEventKind.TOOL_COMPLETED, AgentEventKind.TOOL_FAILED}:
            state.phase = "failed" if event.kind is AgentEventKind.TOOL_FAILED else "completed"
            state.duration = self._event_duration(data)
            state.started_at = None
            state.content = self._optional_text(data.get("content"), allow_empty=True)
            state.is_error = (
                event.kind is AgentEventKind.TOOL_FAILED or data.get("is_error") is True
            )
            raw_metadata = data.get("metadata")
            state.metadata = dict(raw_metadata) if isinstance(raw_metadata, Mapping) else None
            state.artifact_id = self._artifact_id_from_metadata(raw_metadata)
            state.artifact_content = None
            state.artifact_read_truncated = False
            state.artifact_loading = False
            state.artifact_unavailable = False
            raw_changes = data.get("workspace_changes")
            state.workspace_changes = (
                dict(raw_changes) if isinstance(raw_changes, Mapping) else None
            )
            state.expanded = (
                state.is_error
                or state.workspace_changes is not None
                or state.name in {"apply_patch", "search_replace"}
            )
        self._refresh_tool_feedback(state)

    def _start_tool_feedback(
        self,
        event: AgentEvent,
        *,
        hosted: bool,
    ) -> ToolFeedbackState:
        data = event.data
        call_id = self._tool_event_id(event)
        raw_arguments = data.get("arguments")
        arguments = dict(raw_arguments) if isinstance(raw_arguments, Mapping) else {}
        state = ToolFeedbackState(
            call_id=call_id,
            name=self._field(data, "name"),
            arguments=arguments,
            entry_index=len(self._entries),
            hosted=hosted,
        )
        self._tool_feedback_by_call[(hosted, call_id)] = state
        self._tool_feedback_by_entry[state.entry_index] = state
        content = self._tool_feedback_body(state).plain
        self._write_entry("tool", content, tool_state=state)
        return state

    def _find_or_start_tool_feedback(
        self,
        event: AgentEvent,
        *,
        hosted: bool,
    ) -> ToolFeedbackState:
        raw_id = event.data.get("id")
        if isinstance(raw_id, str) and raw_id:
            state = self._tool_feedback_by_call.get((hosted, raw_id))
            if state is not None:
                return state
        name = self._optional_text(event.data.get("name"))
        candidates = (
            state
            for state in self._tool_feedback_by_entry.values()
            if state.hosted is hosted
            and (name is None or state.name == name)
            and state.phase not in {"completed", "failed", "permission_denied", "approval_denied"}
        )
        latest = max(candidates, key=lambda state: state.entry_index, default=None)
        return latest if latest is not None else self._start_tool_feedback(event, hosted=hosted)

    @staticmethod
    def _tool_event_id(event: AgentEvent) -> str:
        raw_id = event.data.get("id")
        return raw_id if isinstance(raw_id, str) and raw_id else f"event-{event.sequence}"

    @staticmethod
    def _optional_text(value: object, *, allow_empty: bool = False) -> str | None:
        if not isinstance(value, str):
            return None
        if value or allow_empty:
            return value
        return None

    @staticmethod
    def _artifact_id_from_metadata(metadata: object) -> str | None:
        if not isinstance(metadata, Mapping):
            return None
        value = metadata.get("output_artifact_id")
        if not isinstance(value, str) or not value.strip() or "\x00" in value or len(value) > 128:
            return None
        return value

    def _refresh_tool_feedback(self, state: ToolFeedbackState) -> None:
        if state.entry_index >= len(self._entries):
            return
        transcript = self.query_one("#transcript", VerticalScroll)
        follow = transcript.is_vertical_scroll_end
        body = self._tool_feedback_body(state)
        self._entries[state.entry_index] = TranscriptEntry("tool", body.plain)
        widget = self._entry_widgets[state.entry_index]
        widget.update(self._render_tool_feedback(state, body=body))
        if isinstance(widget, ToolFeedbackMessage):
            self._configure_tool_feedback_widget(widget, state)
        if follow:
            transcript.scroll_end(animate=False)

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
        if not isinstance(value, str) or not value:
            return "—"
        rendered = " ".join(NeuroCodeApp._safe_tool_text(value).split())
        return rendered if len(rendered) <= limit else f"{rendered[: limit - 1]}…"

    @staticmethod
    def _safe_tool_text(value: str) -> str:
        normalized = _ANSI_ESCAPE.sub("", value.replace("\r\n", "\n").replace("\r", "\n"))
        printable = "".join(
            character if character in {"\n", "\t"} or ord(character) >= 32 else "�"
            for character in normalized
        )
        return redact_sensitive_text(printable)

    @classmethod
    def _event_duration(cls, data: Mapping[str, Any]) -> str:
        value = data.get("duration_seconds")
        if not isinstance(value, int | float) or isinstance(value, bool) or value < 0:
            return "—"
        seconds = float(value)
        if seconds < 0.001:
            return "<1ms"
        if seconds < 1:
            return f"{seconds * 1000:.0f}ms"
        if seconds < 60:
            return f"{seconds:.1f}s"
        minutes, remainder = divmod(round(seconds), 60)
        return f"{minutes}m {remainder:02d}s"

    @classmethod
    def _tool_invocation(cls, name: str, raw_arguments: object) -> str:
        if not isinstance(raw_arguments, Mapping):
            return name
        preferred_keys = {
            "bash": ("command",),
            "read_file": ("path",),
            "list_dir": ("path",),
            "grep": ("query", "path"),
            "search_replace": ("path",),
            "apply_patch": ("path",),
            "task_output": ("task_id",),
        }.get(name, ("path", "query", "pattern", "task_id"))
        values = [
            cls._bounded_inline(raw_arguments.get(key), limit=100)
            for key in preferred_keys
            if isinstance(raw_arguments.get(key), str) and raw_arguments.get(key)
        ]
        return f"{name}({', '.join(values)})" if values else name

    def _tool_feedback_body(self, state: ToolFeedbackState) -> Text:
        if state.phase == "completed":
            marker, marker_style = _SUCCESS_MARK, TOOL_COMPLETE_STYLE
        elif state.phase in {"failed", "permission_denied", "approval_denied"}:
            marker, marker_style = _ERROR_MARK, ERROR_TEXT_STYLE
        else:
            marker, marker_style = "…", TOOL_ACTIVE_STYLE
        compact_summary = self._compact_read_summary(state)
        if compact_summary is not None and not state.expanded:
            body = Text(overflow="fold")
            body.append(f"{marker} ", style=marker_style)
            body.append(compact_summary, style=TOOL_DETAIL_STYLE)
            if state.phase == "completed" and state.duration is not None:
                body.append(f" · {state.duration}", style=TOOL_META_STYLE)
            return body

        body = Text(overflow="fold")
        body.append(f"{marker} ", style=marker_style)
        if state.hosted:
            body.append(ui_text(self._language, "tool.card.hosted"), style=TOOL_META_STYLE)
            body.append(" ")
        invocation = self._tool_invocation(state.name, state.arguments)
        body.append(invocation, style=TOOL_TITLE_STYLE)

        if state.permission_effect == "allow":
            self._append_tool_line(
                body,
                "├",
                ui_text(
                    self._language,
                    "tool.card.allowed",
                    reason=self._bounded_inline(state.permission_reason),
                ),
                style=TOOL_COMPLETE_STYLE,
            )
        elif state.permission_effect == "ask":
            if state.approval_outcome is not None:
                outcome = ui_text(
                    self._language,
                    f"tool.approval.{self._known_approval_outcome(state.approval_outcome)}",
                )
                self._append_tool_line(
                    body,
                    "├",
                    ui_text(self._language, "tool.card.approval", outcome=outcome),
                    style=(
                        TOOL_COMPLETE_STYLE
                        if state.approval_effect == "allow"
                        else ERROR_TEXT_STYLE
                    ),
                )
            elif state.phase == "awaiting_approval":
                self._append_tool_line(
                    body,
                    "├",
                    ui_text(self._language, "tool.card.awaiting_approval"),
                    style=WAITING_STYLE,
                )
            else:
                self._append_tool_line(
                    body,
                    "├",
                    ui_text(
                        self._language,
                        "tool.card.approval_required",
                        reason=self._bounded_inline(state.permission_reason),
                    ),
                    style=WAITING_STYLE,
                )

        change_report = self._tool_change_report(state)
        details_available = self._tool_details_available(state, change_report)
        if state.content is not None and (state.content or change_report is None):
            self._append_tool_output(body, state, expanded=state.expanded)
        if change_report is not None:
            self._append_tool_changes(body, change_report, expanded=state.expanded)
        if details_available:
            self._append_tool_line(
                body,
                "├",
                ui_text(
                    self._language,
                    (
                        "tool.card.details_expanded"
                        if state.expanded
                        else "tool.card.details_collapsed"
                    ),
                ),
                style=TOOL_ACTIVE_STYLE,
            )

        if state.phase == "running":
            running_label = (
                "tool.card.waiting"
                if state.name in {"wait_tasks", "task_output", "wait_for_tasks"}
                else "tool.card.running"
            )
            self._append_tool_line(
                body,
                "├",
                ui_text(self._language, running_label),
                style=WAITING_STYLE,
            )
            body.append(
                f" · {self._running_tool_duration(state)}",
                style=TOOL_META_STYLE,
            )
        if state.phase == "completed":
            duration = state.duration or "—"
            self._append_tool_line(
                body,
                "└",
                ui_text(
                    self._language,
                    "tool.card.completed",
                    duration=duration,
                ),
                style=TOOL_COMPLETE_STYLE,
            )
            body.stylize(TOOL_META_STYLE, len(body.plain) - len(duration), len(body.plain))
        elif state.phase == "failed":
            duration = state.duration or "—"
            self._append_tool_line(
                body,
                "└",
                ui_text(
                    self._language,
                    "tool.card.failed",
                    duration=duration,
                ),
                style=ERROR_TEXT_STYLE,
            )
            body.stylize(TOOL_META_STYLE, len(body.plain) - len(duration), len(body.plain))
        elif state.phase in {"permission_denied", "approval_denied"}:
            reason = state.approval_reason or state.permission_reason
            self._append_tool_line(
                body,
                "└",
                ui_text(
                    self._language,
                    "tool.card.denied",
                    reason=self._bounded_inline(reason),
                ),
                style=ERROR_TEXT_STYLE,
            )
        return body

    def _compact_read_summary(self, state: ToolFeedbackState) -> str | None:
        if (
            state.hosted
            or state.name not in _COMPACT_READ_TOOLS
            or state.is_error
            or state.phase
            in {
                "running",
                "failed",
                "permission_denied",
                "approval_denied",
                "awaiting_approval",
            }
        ):
            return None
        key = "completed" if state.phase == "completed" else "running"
        if state.name == "grep":
            query = self._bounded_inline(state.arguments.get("query"), limit=80)
            path = self._bounded_inline(state.arguments.get("path"), limit=80)
            return ui_text(
                self._language,
                f"tool.compact.grep.{key}",
                query=query,
                path=path,
            )
        target_key = "name" if state.name == "skill" else "path"
        target = self._bounded_inline(state.arguments.get(target_key), limit=120)
        return ui_text(
            self._language,
            f"tool.compact.{state.name}.{key}",
            target=target,
        )

    def _running_tool_duration(self, state: ToolFeedbackState) -> str:
        if state.started_at is None:
            return "—"
        return self._event_duration({"duration_seconds": max(0.0, monotonic() - state.started_at)})

    @staticmethod
    def _append_tool_line(body: Text, connector: str, content: str, *, style: str) -> None:
        body.append("\n")
        body.append(f"{connector} ", style=TOOL_GUIDE_STYLE)
        body.append(content, style=style)

    @staticmethod
    def _known_approval_outcome(outcome: str) -> str:
        return outcome if outcome in {"allow_once", "allow_session", "deny"} else "unknown"

    def _tool_details_available(
        self,
        state: ToolFeedbackState,
        change_report: Mapping[str, Any] | None,
    ) -> bool:
        if state.content is not None and self._safe_tool_text(state.content).strip():
            return True
        if (
            state.artifact_id is not None
            and self._tool_output_artifact_service is not None
            and self._runner.session_id is not None
        ):
            return True
        if change_report is None:
            return False
        raw_files = change_report.get("files")
        if not isinstance(raw_files, Sequence) or isinstance(raw_files, str | bytes):
            return False
        return any(
            isinstance(change, Mapping)
            and isinstance(change.get("diff"), str)
            and bool(self._safe_tool_text(change["diff"]).strip())
            for change in raw_files
        )

    def _configure_tool_feedback_widget(
        self,
        widget: ToolFeedbackMessage,
        state: ToolFeedbackState,
    ) -> None:
        available = self._tool_details_available(state, self._tool_change_report(state))
        widget.can_focus = available
        widget.set_class(available, "tool-interactive")
        widget.set_class(available and state.expanded, "tool-expanded")
        widget.tooltip = (
            ui_text(
                self._language,
                ("tool.card.details_expanded" if state.expanded else "tool.card.details_collapsed"),
            )
            if available
            else None
        )

    def _append_tool_output(
        self,
        body: Text,
        state: ToolFeedbackState,
        *,
        expanded: bool,
    ) -> None:
        content = (
            state.artifact_content
            if expanded and state.artifact_content is not None
            else state.content or ""
        )
        lines, total_lines, omitted_lines, truncated = self._bounded_tool_preview(content)
        if not lines:
            heading = ui_text(
                self._language,
                "tool.card.error_empty" if state.is_error else "tool.card.output_empty",
            )
        else:
            heading = ui_text(
                self._language,
                "tool.card.error" if state.is_error else "tool.card.output",
                lines=total_lines,
            )
        self._append_tool_line(
            body,
            "├",
            heading,
            style=ERROR_TEXT_STYLE if state.is_error else TOOL_META_STYLE,
        )
        if not expanded:
            return
        for line in lines:
            body.append("\n│   ", style=TOOL_GUIDE_STYLE)
            body.append(line, style=TOOL_DETAIL_STYLE)
        if omitted_lines:
            body.append("\n│   ", style=TOOL_GUIDE_STYLE)
            body.append(
                ui_text(self._language, "tool.card.lines_omitted", count=omitted_lines),
                style=f"italic {TOOL_META_STYLE}",
            )
        if truncated:
            body.append("\n│   ", style=TOOL_GUIDE_STYLE)
            body.append(
                ui_text(self._language, "tool.card.preview_truncated"),
                style=f"italic {TOOL_META_STYLE}",
            )
        if expanded and state.artifact_id is not None:
            if state.artifact_loading:
                self._append_tool_line(
                    body,
                    "├",
                    ui_text(self._language, "tool.card.artifact_loading"),
                    style=TOOL_META_STYLE,
                )
            elif state.artifact_unavailable:
                self._append_tool_line(
                    body,
                    "├",
                    ui_text(self._language, "tool.card.artifact_unavailable"),
                    style=TOOL_META_STYLE,
                )
            elif state.artifact_read_truncated:
                self._append_tool_line(
                    body,
                    "├",
                    ui_text(self._language, "tool.card.artifact_read_truncated"),
                    style=f"italic {TOOL_META_STYLE}",
                )

    @classmethod
    def _bounded_tool_preview(cls, content: str) -> tuple[tuple[str, ...], int, int, bool]:
        safe = cls._safe_tool_text(content)
        raw_lines = safe.splitlines()
        total_lines = len(raw_lines)
        omitted_lines = 0
        if total_lines > _TOOL_OUTPUT_MAX_LINES:
            head_count = _TOOL_OUTPUT_MAX_LINES - 10
            selected = [*raw_lines[:head_count], *raw_lines[-10:]]
            omitted_lines = total_lines - len(selected)
        else:
            selected = raw_lines

        rendered: list[str] = []
        characters = 0
        truncated = False
        for line in selected:
            remaining = _TOOL_OUTPUT_MAX_CHARACTERS - characters
            if remaining <= 0:
                truncated = True
                break
            if len(line) > remaining:
                rendered.append(f"{line[: max(0, remaining - 1)]}…")
                truncated = True
                break
            rendered.append(line)
            characters += len(line) + 1
        return tuple(rendered), total_lines, omitted_lines, truncated

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

    def _append_tool_changes(
        self,
        body: Text,
        report: Mapping[str, Any],
        *,
        expanded: bool,
    ) -> None:
        raw_files = report.get("files")
        if not isinstance(raw_files, Sequence) or isinstance(raw_files, str | bytes):
            return
        files = [item for item in raw_files if isinstance(item, Mapping)]
        visible = files[:_TOOL_DIFF_MAX_FILES]
        for change in visible:
            path = self._bounded_inline(change.get("path"), limit=180)
            status = change.get("status")
            additions = self._non_negative_int(change.get("additions"))
            deletions = self._non_negative_int(change.get("deletions"))
            status_key = status if status in {"created", "modified", "deleted"} else "modified"
            summary = ui_text(
                self._language,
                f"tool.card.change.{status_key}",
                path=path,
                additions=additions,
                deletions=deletions,
            )
            content_start = len(body) + 3
            self._append_tool_line(body, "├", summary, style=TOOL_TITLE_STYLE)
            body.highlight_words((path,), style=TOOL_TITLE_STYLE, case_sensitive=True)
            addition_text = f"+{additions}"
            addition_offset = summary.find(addition_text)
            if addition_offset >= 0:
                body.stylize(
                    DIFF_SUMMARY_ADDITION_STYLE,
                    content_start + addition_offset,
                    content_start + addition_offset + len(addition_text),
                )
            deletion_text = f"-{deletions}"
            deletion_offset = summary.find(deletion_text)
            if deletion_offset >= 0:
                body.stylize(
                    DIFF_SUMMARY_DELETION_STYLE,
                    content_start + deletion_offset,
                    content_start + deletion_offset + len(deletion_text),
                )

            raw_diff = change.get("diff")
            if expanded and isinstance(raw_diff, str) and raw_diff:
                diff_lines, _, omitted, truncated = self._bounded_tool_preview(raw_diff)
                for line in diff_lines:
                    body.append("\n│   ", style=TOOL_GUIDE_STYLE)
                    body.append(line, style=self._diff_line_style(line))
                if omitted or truncated or change.get("diff_truncated") is True:
                    body.append("\n│   ", style=TOOL_GUIDE_STYLE)
                    body.append(
                        ui_text(self._language, "tool.card.diff_truncated"),
                        style=f"italic {TOOL_META_STYLE}",
                    )
            hidden_reason = change.get("hidden_reason")
            if isinstance(hidden_reason, str):
                known_reason = (
                    hidden_reason
                    if hidden_reason in {"sensitive", "large", "budget", "binary", "redacted"}
                    else "unavailable"
                )
                body.append("\n│   ", style=TOOL_GUIDE_STYLE)
                body.append(
                    ui_text(self._language, f"tool.card.hidden.{known_reason}"),
                    style=f"italic {TOOL_META_STYLE}",
                )

        omitted_files = self._non_negative_int(report.get("omitted_files")) + max(
            0, len(files) - len(visible)
        )
        if omitted_files:
            self._append_tool_line(
                body,
                "├",
                ui_text(self._language, "tool.card.files_omitted", count=omitted_files),
                style=f"italic {TOOL_META_STYLE}",
            )
        if report.get("scan_limited") is True:
            self._append_tool_line(
                body,
                "├",
                ui_text(self._language, "tool.card.scan_limited"),
                style=f"italic {TOOL_META_STYLE}",
            )

    @staticmethod
    def _non_negative_int(value: object) -> int:
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0

    @staticmethod
    def _diff_line_style(line: str) -> str:
        if line.startswith("@@"):
            return DIFF_HUNK_STYLE
        if line.startswith(("+++", "---")):
            return DIFF_FILE_STYLE
        if line.startswith("+"):
            return DIFF_ADDITION_STYLE
        if line.startswith("-"):
            return DIFF_DELETION_STYLE
        return DIFF_CONTEXT_STYLE

    async def _dispatch_slash_command(self, raw: str) -> None:
        command, _, arguments = raw[1:].partition(" ")
        command = command.casefold()
        if command == "plan":
            description = arguments.strip()
            await self._apply_interaction_mode(InteractionMode.PLAN)
            if description and self._interaction_mode is InteractionMode.PLAN:
                self._submit_prompt(description)
            return
        if command in {"view-plan", "show-plan"}:
            if arguments.strip():
                self._write_ui_entry("error", "command.arguments", command=command)
                return
            await self._show_plan()
            return
        if command in {"comment-plan", "plan-comment"}:
            await self._add_plan_comment(arguments)
            return
        if command in {"execute-plan", "run-plan"}:
            if arguments.strip():
                self._write_ui_entry("error", "command.arguments", command=command)
                return
            await self._execute_plan()
            return
        if command in {"schedule-plan", "queue-plan"}:
            if arguments.strip():
                self._write_ui_entry("error", "command.arguments", command=command)
                return
            await self._schedule_plan()
            return
        if command == "mode":
            mode_value = arguments.strip()
            if not mode_value:
                self._write_ui_entry(
                    "system",
                    "mode.current",
                    mode=self._interaction_mode.value,
                    modes=", ".join(mode.value for mode in InteractionMode),
                )
                return
            try:
                mode = InteractionMode(mode_value.casefold())
            except ValueError:
                self._write_ui_entry(
                    "error",
                    "mode.invalid",
                    value=mode_value,
                    modes=", ".join(mode.value for mode in InteractionMode),
                )
                return
            await self._apply_interaction_mode(mode)
            return
        if command in {"effort", "reasoning"}:
            effort_value = arguments.strip()
            if not effort_value:
                await self._select_reasoning_effort(None)
                return
            try:
                effort = ReasoningEffort(effort_value.casefold())
            except ValueError:
                self._write_ui_entry(
                    "error",
                    "effort.invalid",
                    value=effort_value,
                    levels=", ".join(effort.value for effort in ReasoningEffort),
                )
                return
            await self._select_reasoning_effort(effort)
            return
        if command in {"model", "provider"}:
            await self._select_provider(arguments.strip() or None)
            return
        if command in {"resume", "sessions"}:
            requested_session = arguments.strip() or None
            if command == "sessions":
                await self._select_session(None, query=requested_session)
            else:
                await self._select_session(requested_session)
            return
        if command in {"rename", "title"}:
            await self._rename_session(arguments)
            return
        if command == "tasks":
            if arguments.strip():
                self._write_ui_entry("error", "command.tasks_arguments")
                return
            await self._show_tasks()
            return
        if command == "subagents":
            normalized_arguments = arguments.strip()
            if not normalized_arguments:
                await self._show_subagent_relationships()
            else:
                await self._run_subagent_relationship_action(normalized_arguments)
            return
        if command in {"auto-wake", "autowake"}:
            await self._apply_background_task_wake_policy(arguments)
            return
        if command == "view-task":
            await self._show_session_task(arguments.strip())
            return
        if command == "run-task":
            task_id = arguments.strip()
            if not task_id or " " in task_id:
                self._write_ui_entry("error", "tasks.run.usage")
                return
            await self._run_queued_task(task_id)
            return
        if command == "subagent":
            await self._run_read_only_subagent(arguments)
            return
        if command in {"setting", "settings"}:
            if arguments.strip():
                self._write_ui_entry("error", "command.arguments", command=command)
                return
            await self.action_open_settings()
            return
        if arguments.strip():
            self._write_ui_entry("error", "command.arguments", command=command)
            return
        if command in {"quit", "exit"}:
            self.exit()
        elif command == "cancel":
            self.action_cancel_turn()
        elif command == "clear":
            self.action_clear_transcript()
        elif command == "help":
            self._write_ui_entry("system", "command.help")
        elif command == "status":
            session_id = self._runner.session_id or ui_text(self._language, "command.not_created")
            profile = (
                ui_text(
                    self._language,
                    "command.profile",
                    profile=self._provider_controller.selected_profile,
                )
                if self._provider_controller is not None
                else ""
            )
            self._write_ui_entry(
                "system",
                "command.status",
                provider=self._provider_name,
                model=self._model_name,
                effort=self._reasoning_effort_summary(),
                context=self._context_usage_summary(),
                mode=self._interaction_mode_summary(),
                session=session_id,
                profile=profile,
                cwd=self._cwd,
            )
        else:
            self._write_ui_entry("error", "command.unknown", command=command)

    async def _run_read_only_subagent(self, raw_prompt: str) -> None:
        """Start one explicit, bounded read-only child without parent transcript reuse.

        启动一次明确且有界的只读子代理运行,不复用父会话 transcript.
        """

        prompt = raw_prompt.strip()
        if not prompt:
            self._write_ui_entry("error", "subagent.usage")
            return
        if self._read_only_subagent_service is None:
            self._write_ui_entry("error", "subagent.unavailable")
            return
        session_id = self._runner.session_id
        if session_id is None:
            self._write_ui_entry("error", "subagent.session_required")
            return
        if self._turn_worker is not None and self._turn_worker.is_running:
            self._write_ui_entry("error", "turn.running")
            return
        self._write_ui_entry("status", "subagent.started")
        self._turn_worker = self.run_worker(
            self._run_read_only_subagent_task(session_id, prompt),
            name="agent-read-only-subagent",
            group="agent",
            exclusive=True,
            exit_on_error=False,
        )

    async def _run_read_only_subagent_task(self, session_id: str, prompt: str) -> None:
        service = self._read_only_subagent_service
        if service is None:
            return
        try:
            projection = await service.run_subagent(
                RunSubagentRequest(session_id, prompt),
            )
        except asyncio.CancelledError:
            self._write_ui_entry("status", "subagent.cancelled")
            raise
        except Exception as error:
            self._write_ui_entry(
                "error",
                "subagent.failed",
                error=self._safe_tool_text(str(error)),
            )
            return

        status_label = ui_text(
            self._language,
            f"tasks.status.{projection.status.value}",
        )
        if projection.status is SessionTaskStatus.COMPLETED:
            self._write_ui_entry(
                "status",
                "subagent.completed",
                steps=projection.steps,
            )
        else:
            self._write_ui_entry(
                "error",
                "subagent.finished",
                status=status_label,
                steps=projection.steps,
            )
        if projection.truncated:
            self._write_ui_entry("status", "subagent.truncated")
        if projection.response:
            self._write_entry("assistant", projection.response)

    async def _apply_background_task_wake_policy(self, arguments: str) -> None:
        value = arguments.strip().casefold()
        if not value:
            self._write_ui_entry(
                "system",
                "background_wake.current",
                policy=self._background_task_wake_policy_label(),
            )
            return
        policy_values = {
            "on": BackgroundTaskWakePolicy.ENABLED,
            "enabled": BackgroundTaskWakePolicy.ENABLED,
            "off": BackgroundTaskWakePolicy.DISABLED,
            "disabled": BackgroundTaskWakePolicy.DISABLED,
        }
        policy = policy_values.get(value)
        if policy is None:
            self._write_ui_entry(
                "error",
                "background_wake.invalid",
                value=value,
            )
            return
        if (
            policy is self._background_task_wake_policy
            and policy is self._background_task_wake_policy_override
        ):
            self._write_ui_entry(
                "status",
                "background_wake.already_selected",
                policy=self._background_task_wake_policy_label(),
            )
            return
        self._background_task_wake_policy_override = policy
        self._background_task_wake_policy = policy
        self._write_ui_entry(
            "status",
            "background_wake.changed",
            policy=self._background_task_wake_policy_label(),
        )
        if policy is BackgroundTaskWakePolicy.ENABLED:
            await self._poll_background_tasks()

    def _background_task_wake_policy_label(self) -> str:
        return ui_text(
            self._language,
            f"background_wake.policy.{self._background_task_wake_policy.value}",
        )

    def action_clear_transcript(self) -> None:
        transcript = self.query_one("#transcript", VerticalScroll)
        transcript.remove_children(tuple(self._entry_widgets))
        self._entries.clear()
        self._entry_widgets.clear()
        self._tool_feedback_by_call.clear()
        self._tool_feedback_by_entry.clear()
        self._plan_entry_index = None
        self._plan_comments = ()
        self._queued_interjections.clear()
        self._write_ui_entry("system", "transcript.cleared")

    def action_copy_prompt(self) -> None:
        """Copy selected prompt text without changing turn control behavior.

        复制提示框中的选中文本,不改变轮次控制行为.
        """

        prompt = self.query_one("#prompt", Input)
        if prompt.has_focus and prompt.selected_text:
            prompt.action_copy()

    def action_cancel_turn(self) -> None:
        if isinstance(self.screen, PermissionApprovalScreen):
            self.screen.action_deny()
            return
        if isinstance(self.screen, ProviderSelectionScreen):
            self.screen.action_cancel()
            return
        if isinstance(self.screen, ReasoningEffortScreen):
            self.screen.action_cancel()
            return
        if isinstance(self.screen, SessionSelectionScreen):
            self.screen.action_cancel()
            return
        if isinstance(
            self.screen,
            (
                SettingsScreen,
                LanguageSettingsScreen,
                NetworkProxySettingsScreen,
                BackgroundWakeSettingsScreen,
                ProviderSettingsScreen,
            ),
        ):
            self.screen.action_cancel()
            return
        if self._turn_worker is not None and self._turn_worker.is_running:
            self._write_ui_entry("status", "turn.cancel_requested")
            self._turn_worker.cancel()
            return
        prompt = self.query_one("#prompt", Input)
        if prompt.has_focus and prompt.selected_text:
            prompt.action_copy()
            return
        if prompt.value:
            prompt.value = ""
            self._write_ui_entry("status", "turn.draft_cleared")
        else:
            self._write_ui_entry("status", "turn.none_running")

    async def action_select_provider(self) -> None:
        await self._select_provider(None)

    async def action_select_reasoning_effort(self) -> None:
        await self._select_reasoning_effort(None)

    async def action_cycle_interaction_mode(self) -> None:
        if isinstance(self.screen, ModalScreen):
            self.screen.focus_previous()
            return
        await self._apply_interaction_mode(self._interaction_mode.next)

    async def action_select_session(self) -> None:
        await self._select_session(None)

    async def action_open_settings(self) -> None:
        self.push_screen(
            SettingsScreen(
                self._language,
                language=self._language,
                provider_settings_available=(
                    self._managed_provider_settings is not None
                    and self._provider_settings_store is not None
                ),
            ),
            self._settings_category_selected,
        )

    async def _settings_category_selected(self, category: str | None) -> None:
        if category == "language":
            self.push_screen(
                LanguageSettingsScreen(self._language, language=self._language),
                self._language_settings_selected,
            )
            return
        if category == "providers":
            if self._managed_provider_settings is None or self._provider_settings_store is None:
                return
            self.push_screen(
                ProviderSettingsScreen(
                    language=self._language,
                    provider_settings=self._managed_provider_settings,
                    provider_settings_store=self._provider_settings_store,
                    provider_catalog=self._provider_catalog,
                ),
                self._provider_settings_selected,
            )
            return
        if category == "network":
            if self._managed_provider_settings is None or self._provider_settings_store is None:
                return
            self.push_screen(
                NetworkProxySettingsScreen(
                    language=self._language,
                    provider_settings=self._managed_provider_settings,
                    provider_settings_store=self._provider_settings_store,
                ),
                self._network_proxy_settings_selected,
            )
            return
        if category == "background-wake":
            if self._managed_provider_settings is None or self._provider_settings_store is None:
                return
            self.push_screen(
                BackgroundWakeSettingsScreen(
                    language=self._language,
                    provider_settings=self._managed_provider_settings,
                    provider_settings_store=self._provider_settings_store,
                ),
                self._background_wake_settings_selected,
            )

    async def _provider_settings_selected(
        self,
        result: ProviderSettingsSubmission | None,
    ) -> None:
        if result is not None:
            self.exit(return_code=TUI_RELOAD_PROVIDER_SETTINGS)
            return
        await self.action_open_settings()

    async def _network_proxy_settings_selected(
        self,
        settings: ManagedProviderSettings | None,
    ) -> None:
        if settings is not None:
            self.exit(return_code=TUI_RELOAD_PROVIDER_SETTINGS)
            return
        await self.action_open_settings()

    async def _background_wake_settings_selected(
        self,
        settings: ManagedProviderSettings | None,
    ) -> None:
        if settings is not None:
            self.exit(return_code=TUI_RELOAD_PROVIDER_SETTINGS)
            return
        await self.action_open_settings()

    async def _select_reasoning_effort(
        self,
        requested: ReasoningEffort | None,
    ) -> None:
        if self._reasoning_controller is None:
            self._write_ui_entry("error", "effort.unavailable")
            return
        if self._turn_worker is not None and self._turn_worker.is_running:
            self._write_ui_entry("error", "effort.switch_running")
            return
        if requested is None:
            self.push_screen(
                ReasoningEffortScreen(
                    self._reasoning_effort,
                    language=self._language,
                ),
                self._reasoning_effort_selected,
            )
            return
        await self._apply_reasoning_effort(requested)

    async def _reasoning_effort_selected(
        self,
        effort: ReasoningEffort | None,
    ) -> None:
        if effort is not None:
            await self._apply_reasoning_effort(effort)

    async def _apply_reasoning_effort(self, effort: ReasoningEffort) -> None:
        assert self._reasoning_controller is not None
        try:
            result = await self._reasoning_controller.set_reasoning_effort(effort)
        except Exception as error:
            self._write_entry("error", f"{type(error).__name__}: {error}")
            return

        self._reasoning_effort = result.requested
        self._effective_reasoning_effort = result.effective
        self._refresh_runtime_bar()
        if not result.changed:
            self._write_ui_entry(
                "status",
                "effort.already_selected",
                glyph=result.requested.glyph,
                effort=result.requested.value,
            )
            return
        if result.requested is ReasoningEffort.ULTRACODE:
            self._write_ui_entry(
                "status",
                "effort.changed_fallback",
                requested=result.requested.value,
                effective=result.effective.value,
            )
        else:
            self._write_ui_entry(
                "status",
                "effort.changed",
                glyph=result.requested.glyph,
                effort=result.requested.value,
            )
        if self._ui_preferences is not None:
            try:
                await self._ui_preferences.save_reasoning_effort(result.requested)
            except Exception as error:
                self._write_ui_entry(
                    "error",
                    "effort.save_failed",
                    error=f"{type(error).__name__}: {error}",
                )

    async def _apply_interaction_mode(self, mode: InteractionMode) -> None:
        if self._interaction_mode_controller is None:
            self._write_ui_entry("error", "mode.unavailable")
            return
        if self._turn_worker is not None and self._turn_worker.is_running:
            self._write_ui_entry("error", "mode.switch_running")
            return
        try:
            result = await self._interaction_mode_controller.set_interaction_mode(mode)
        except Exception as error:
            self._write_entry("error", f"{type(error).__name__}: {error}")
            return

        self._interaction_mode = result.requested
        self._auto_mode_unrestricted = result.auto_unrestricted
        self._refresh_runtime_bar()
        if not result.changed:
            self._write_ui_entry(
                "status",
                "mode.already_selected",
                glyph=result.requested.glyph,
                mode=result.requested.value,
            )
            return
        key = "mode.changed_auto_limited" if result.limited_auto else "mode.changed"
        self._write_ui_entry(
            "status",
            key,
            glyph=result.requested.glyph,
            mode=result.requested.value,
        )
        if self._ui_preferences is not None:
            try:
                await self._ui_preferences.save_interaction_mode(result.requested)
            except Exception as error:
                self._write_ui_entry(
                    "error",
                    "mode.save_failed",
                    error=f"{type(error).__name__}: {error}",
                )

    async def _execute_plan(self) -> None:
        controller = self._plan_controller
        if controller is None:
            self._write_ui_entry("error", "plan.execution_unavailable")
            return
        plan = controller.plan
        self._plan = plan
        if plan is None:
            self._write_ui_entry("status", "plan.none")
            return
        if self._turn_worker is not None and self._turn_worker.is_running:
            self._write_ui_entry("error", "turn.running")
            return
        await self._apply_interaction_mode(InteractionMode.ACCEPT_EDITS)
        if self._interaction_mode is not InteractionMode.ACCEPT_EDITS:
            return
        self._write_ui_entry("user", "plan.execution_user")
        self._assistant_parts.clear()
        self._first_token_seen = False
        self._reasoning_announced = False
        self._turn_completion = None
        self._terminal_execution_status = None
        self._terminal_execution_recoverable = False
        self._finalizing = False
        self._turn_usage_reported = False
        self._begin_pending_assistant()
        self._turn_worker = self.run_worker(
            self._run_plan_execution(),
            name="agent-plan-execution",
            group="agent",
            exclusive=True,
            exit_on_error=False,
        )

    async def _schedule_plan(self) -> None:
        controller = self._plan_controller
        if controller is None:
            self._write_ui_entry("error", "plan.execution_unavailable")
            return
        if controller.plan is None:
            self._write_ui_entry("status", "plan.none")
            return
        if self._turn_worker is not None and self._turn_worker.is_running:
            self._write_ui_entry("error", "turn.running")
            return
        try:
            service = self._plan_scheduling_service
            if service is not None:
                task = await service.schedule_plan(SchedulePlanRequest())
            else:
                task = await controller.schedule_plan()
        except Exception as error:
            self._write_entry("error", f"{type(error).__name__}: {error}")
            return
        self._write_ui_entry("status", "plan.scheduled", task_id=task.task_id)

    async def _run_queued_task(self, task_id: str) -> None:
        controller = self._plan_controller
        task_controller = self._session_task_controller
        if controller is None or task_controller is None:
            self._write_ui_entry("error", "plan.execution_unavailable")
            return
        if self._turn_worker is not None and self._turn_worker.is_running:
            self._write_ui_entry("error", "turn.running")
            return
        try:
            task = await task_controller.get_session_task(task_id)
        except Exception as error:
            self._write_entry("error", f"{type(error).__name__}: {error}")
            return
        if task is None:
            self._write_ui_entry("error", "tasks.run.not_found", task_id=task_id)
            return
        if task.kind is not SessionTaskKind.PLAN_EXECUTION:
            self._write_ui_entry("error", "tasks.run.not_plan", task_id=task_id)
            return
        if task.status is not SessionTaskStatus.QUEUED:
            self._write_ui_entry("error", "tasks.run.not_queued", task_id=task_id)
            return
        await self._apply_interaction_mode(InteractionMode.ACCEPT_EDITS)
        if self._interaction_mode is not InteractionMode.ACCEPT_EDITS:
            return
        self._write_ui_entry("user", "plan.task_execution_user", task_id=task_id)
        self._assistant_parts.clear()
        self._first_token_seen = False
        self._reasoning_announced = False
        self._turn_completion = None
        self._terminal_execution_status = None
        self._terminal_execution_recoverable = False
        self._finalizing = False
        self._turn_usage_reported = False
        self._begin_pending_assistant()
        self._turn_worker = self.run_worker(
            self._run_queued_plan(task_id),
            name="agent-queued-plan-execution",
            group="agent",
            exclusive=True,
            exit_on_error=False,
        )

    async def _language_settings_selected(
        self,
        result: UiLanguage | None,
    ) -> None:
        language = result
        if language is None:
            await self.action_open_settings()
            return
        if language is self._language:
            return
        self._language = language
        self._refresh_localized_interface()
        if self._ui_preferences is not None:
            try:
                await self._ui_preferences.save_language(language)
            except Exception as error:
                self._write_ui_entry(
                    "error",
                    "settings.save_failed",
                    error=f"{type(error).__name__}: {error}",
                )
                return
        self._write_ui_entry(
            "system",
            "settings.changed",
            language=language_name(language, in_language=language),
        )

    async def _select_provider(self, requested: str | None) -> None:
        if self._provider_controller is None:
            self._write_ui_entry("error", "provider.switch_unavailable")
            return
        if self._turn_worker is not None and self._turn_worker.is_running:
            self._write_ui_entry("error", "provider.switch_running")
            return
        profile_name = requested
        if profile_name is None:
            self.push_screen(
                ProviderSelectionScreen(
                    self._provider_controller.profiles,
                    language=self._language,
                ),
                self._provider_selected,
            )
            return
        await self._apply_provider_selection(profile_name)

    async def _provider_selected(self, profile_name: str | None) -> None:
        if profile_name is not None:
            await self._apply_provider_selection(profile_name)

    async def _apply_provider_selection(self, profile_name: str) -> None:
        assert self._provider_controller is not None
        try:
            result = await self._provider_controller.change_provider(
                ChangeProviderRequest(profile_name)
            )
        except Exception as error:
            self._write_entry("error", f"{type(error).__name__}: {error}")
            return

        if (
            self._provider_settings_store is not None
            and self._managed_provider_settings is not None
            and self._managed_provider_settings.profile(profile_name) is not None
        ):
            try:
                self._managed_provider_settings = await self._provider_settings_store.set_default(
                    profile_name
                )
            except Exception as error:
                self._write_ui_entry(
                    "error",
                    "provider.default_save_failed",
                    error=f"{type(error).__name__}: {error}",
                )

        self._provider_name = result.provider_name
        self._model_name = result.model_name
        if self._background_task_wake_policy_override is None:
            self._background_task_wake_policy = (
                self._managed_provider_settings.effective_background_task_wake_policy(
                    result.profile_name
                )
                if self._managed_provider_settings is not None
                else BackgroundTaskWakePolicy.DISABLED
            )
        if self._plan_controller is not None:
            self._plan = self._plan_controller.plan
        self._context_window_tokens = result.context_window_tokens
        if result.changed:
            self._context_used_tokens = 0
            self._context_usage_estimated = True
        self._refresh_runtime_bar()
        if result.changed:
            self._queued_interjections.clear()
            self._reset_background_task_tracking()
            await self._ensure_background_wake_state()
        if not result.changed:
            self._write_ui_entry(
                "status",
                "provider.already_selected",
                profile=result.profile_name,
            )
        elif result.previous_session_id is None:
            self._write_ui_entry(
                "status",
                "provider.switched",
                profile=result.profile_name,
                provider=result.provider_name,
                model=result.model_name,
                stopped=self._stopped_task_note(result.stopped_background_tasks),
            )
        else:
            self._write_ui_entry(
                "status",
                "provider.switched_saved",
                profile=result.profile_name,
                provider=result.provider_name,
                model=result.model_name,
                session_id=result.previous_session_id,
                stopped=self._stopped_task_note(result.stopped_background_tasks),
            )

    async def _select_session(
        self,
        requested: str | None,
        *,
        query: str | None = None,
    ) -> None:
        controller = self._session_selection_owner()
        if controller is None:
            self._write_ui_entry("error", "session.resume_unavailable")
            return
        if self._turn_worker is not None and self._turn_worker.is_running:
            self._write_ui_entry("error", "session.resume_running")
            return
        if requested is not None:
            await self._apply_session_selection(requested)
            return
        try:
            options = await controller.list_sessions(query)
        except Exception as error:
            self._write_entry("error", f"{type(error).__name__}: {error}")
            return
        if not options:
            if query is None:
                self._write_ui_entry("status", "session.none")
            else:
                self._write_ui_entry(
                    "status",
                    "session.none_matching",
                    query=query,
                )
            return
        self.push_screen(
            SessionSelectionScreen(
                options,
                query=query,
                language=self._language,
                search_callback=controller.list_sessions,
            ),
            self._session_selected,
        )

    async def _rename_session(self, title: str) -> None:
        controller = self._session_selection_owner()
        if controller is None:
            self._write_ui_entry("error", "session.rename_unavailable")
            return
        if self._turn_worker is not None and self._turn_worker.is_running:
            self._write_ui_entry("error", "session.rename_running")
            return
        try:
            summary = await controller.rename_session(title)
        except Exception as error:
            self._write_entry("error", f"{type(error).__name__}: {error}")
            return
        self._write_ui_entry(
            "status",
            "session.renamed",
            session_id=summary.id,
            title=summary.title,
        )

    async def _session_selected(self, session_id: str | None) -> None:
        if session_id is not None:
            await self._apply_session_selection(session_id)

    async def _apply_session_selection(self, session_id: str) -> None:
        controller = self._session_selection_owner()
        assert controller is not None
        try:
            result = await controller.select_session(session_id)
        except Exception as error:
            self._write_entry("error", f"{type(error).__name__}: {error}")
            return

        self._provider_name = result.provider_name
        self._model_name = result.model_name
        if self._plan_controller is not None:
            self._plan = self._plan_controller.plan
        self._context_window_tokens = result.context_window_tokens
        if result.changed:
            self._context_used_tokens = estimate_context_tokens(result.items)
            self._context_usage_estimated = True
        self._refresh_runtime_bar()
        if not result.changed:
            self._write_ui_entry(
                "status",
                "session.already_open",
                session_id=result.session_id,
            )
            return

        self._queued_interjections.clear()
        self._reset_background_task_tracking()
        await self._ensure_background_wake_state()
        self._replace_transcript(result.items)
        self._execution_record = self._session_execution_record()
        profile_note = (
            ui_text(
                self._language,
                "session.profile",
                profile=result.profile_name,
            )
            if result.source_profile_match
            else ui_text(
                self._language,
                "session.profile_unavailable",
                profile=result.profile_name,
                source=result.source_provider,
            )
        )
        previous_note = (
            ui_text(
                self._language,
                "session.previous_saved",
                session_id=result.previous_session_id,
            )
            if result.previous_session_id is not None
            else ""
        )
        self._write_ui_entry(
            "system",
            "session.resumed",
            session_id=result.session_id,
            profile_note=profile_note,
            provider=result.provider_name,
            model=result.model_name,
            previous=previous_note,
            stopped=self._stopped_task_note(result.stopped_background_tasks),
        )
        self._write_recoverable_resume_notice(self._execution_record)

    async def _add_plan_comment(self, arguments: str) -> None:
        controller = self._plan_controller
        if controller is None:
            self._write_ui_entry("error", "plan.comment_unavailable")
            return
        plan = controller.plan
        self._plan = plan
        if plan is None:
            self._write_ui_entry("status", "plan.none")
            return
        if self._turn_worker is not None and self._turn_worker.is_running:
            self._write_ui_entry("error", "turn.running")
            return
        raw_index, separator, content = arguments.strip().partition(" ")
        if not separator or not raw_index or not content.strip():
            self._write_ui_entry("error", "plan.comment_usage")
            return
        try:
            step_index = int(raw_index)
        except ValueError:
            self._write_ui_entry("error", "plan.comment_step_invalid", index=raw_index)
            return
        if not 1 <= step_index <= len(plan.steps):
            self._write_ui_entry("error", "plan.comment_step_invalid", index=raw_index)
            return
        try:
            await controller.add_plan_comment(step_index, content)
        except Exception as error:
            self._write_entry("error", f"{type(error).__name__}: {error}")
            return
        self._write_ui_entry("status", "plan.comment_added", index=step_index)
        await self._show_plan()

    async def _show_plan(self) -> None:
        controller = self._plan_controller
        plan = controller.plan if controller is not None else self._plan
        self._plan = plan
        if plan is None:
            self._write_ui_entry("status", "plan.none")
            return
        comments: tuple[PlanComment, ...] = ()
        if controller is not None:
            try:
                comments = await controller.list_plan_comments()
            except Exception as error:
                self._write_entry("error", f"{type(error).__name__}: {error}")
                return
        self._plan_comments = comments
        self._upsert_plan_entry(plan, comments)

    def _render_plan(self, plan: SessionPlan, comments: Sequence[PlanComment] = ()) -> Text:
        body = Text(overflow="fold")
        body.append(
            ui_text(self._language, "plan.heading"),
            style=f"bold {TEXT_PRIMARY}",
        )
        if plan.explanation is not None:
            body.append("\n")
            body.append(
                ui_text(self._language, "plan.purpose", explanation=plan.explanation),
                style=TEXT_SECONDARY,
            )
        for index, step in enumerate(plan.steps, start=1):
            marker, marker_style = {
                PlanStepStatus.COMPLETED: (_SUCCESS_MARK, ACCENT_SUCCESS),
                PlanStepStatus.IN_PROGRESS: (_PROMPT_MARK, ACCENT_CODE),
                PlanStepStatus.PENDING: ("□", TEXT_SECONDARY),
            }[step.status]
            body.append("\n")
            body.append(f"{marker} ", style=marker_style)
            body.append(step.step, style=TEXT_BODY)
            for comment in comments:
                if comment.step_index == index:
                    body.append("\n  · ", style=TEXT_MUTED)
                    body.append(comment.content, style=TEXT_SECONDARY)
        return body

    def _upsert_plan_entry(
        self,
        plan: SessionPlan,
        comments: Sequence[PlanComment] = (),
    ) -> None:
        rendered = self._render_plan(plan, comments)
        index = self._plan_entry_index
        if index is not None and 0 <= index < len(self._entries):
            self._entries[index] = TranscriptEntry("plan", rendered.plain)
            self._entry_widgets[index].update(rendered)
            transcript = self.query_one("#transcript", VerticalScroll)
            if transcript.is_vertical_scroll_end:
                transcript.scroll_end(animate=False)
            return

        transcript = self.query_one("#transcript", VerticalScroll)
        follow = transcript.is_vertical_scroll_end
        widget = ConversationMessage("plan", rendered)
        pending = self._pending_assistant
        if pending is not None and pending.parent is transcript:
            transcript.mount(widget, before=pending)
        else:
            transcript.mount(widget)
        self._entries.append(TranscriptEntry("plan", rendered.plain))
        self._entry_widgets.append(widget)
        self._plan_entry_index = len(self._entries) - 1
        if follow:
            transcript.scroll_end(animate=False)

    async def _show_tasks(self) -> None:
        if self._task_controller is None and self._session_task_controller is None:
            self._write_ui_entry("error", "tasks.unavailable")
            return
        try:
            snapshots = (
                await self._task_controller.list_background_tasks()
                if self._task_controller is not None
                else ()
            )
            session_tasks = (
                await self._session_task_controller.list_session_tasks()
                if self._session_task_controller is not None
                else ()
            )
        except Exception as error:
            self._write_entry("error", f"{type(error).__name__}: {error}")
            return
        if not snapshots and not session_tasks:
            self._write_ui_entry("status", "tasks.none")
            return

        visible = snapshots[-_TASK_LIST_LIMIT:]
        omitted = len(snapshots) - len(visible)
        lines = [self._task_summary(snapshot) for snapshot in visible]
        lines.extend(self._session_task_summary(task) for task in session_tasks[:_TASK_LIST_LIMIT])
        if omitted:
            lines.insert(0, ui_text(self._language, "tasks.omitted", count=omitted))
        self._write_ui_entry(
            "system",
            "tasks.heading",
            lines="\n".join(lines),
        )

    async def _show_session_task(self, task_id: str) -> None:
        if not task_id:
            self._write_ui_entry("error", "tasks.view.usage")
            return
        controller = self._session_task_controller
        if controller is None:
            self._write_ui_entry("error", "tasks.unavailable")
            return
        try:
            task = await controller.get_session_task(task_id)
        except Exception as error:
            self._write_entry("error", f"{type(error).__name__}: {error}")
            return
        if task is None:
            self._write_ui_entry("status", "tasks.view.not_found", task_id=task_id)
            return
        if task.plan_snapshot is None:
            self._write_ui_entry("status", "tasks.view.no_plan", task_id=task.task_id)
            return

        plan = task.plan_snapshot
        finished = (
            ui_text(
                self._language,
                "tasks.session.finished",
                finished=task.finished_at.astimezone().strftime("%H:%M:%S"),
            )
            if task.finished_at is not None
            else ""
        )
        lines = [
            ui_text(self._language, "tasks.view.heading", task_id=task.task_id),
            ui_text(
                self._language,
                "tasks.view.lifecycle",
                kind=ui_text(self._language, f"tasks.kind.{task.kind.value}"),
                status=ui_text(self._language, f"tasks.status.{task.status.value}"),
                started=task.started_at.astimezone().strftime("%H:%M:%S"),
                finished=finished,
            ),
            ui_text(
                self._language,
                "tasks.view.revision",
                fingerprint=plan.fingerprint,
            ),
            ui_text(self._language, "tasks.view.snapshot"),
        ]
        if plan.explanation is not None:
            lines.append(
                ui_text(self._language, "tasks.view.purpose", explanation=plan.explanation)
            )
        lines.extend(
            ui_text(
                self._language,
                "tasks.view.step",
                index=index,
                status=ui_text(self._language, f"plan.status.{step.status.value}"),
                step=step.step,
            )
            for index, step in enumerate(plan.steps, start=1)
        )
        lines.append(ui_text(self._language, "tasks.view.reference"))
        self._write_entry("system", "\n".join(lines))

    async def _show_subagent_relationships(self) -> None:
        """Render bounded child metadata without executing lifecycle actions.

        在不执行生命周期动作的前提下渲染有界子代理元数据.
        """

        controller = self._subagent_relationship_query
        if controller is None:
            self._write_ui_entry("error", "subagents.unavailable")
            return
        session_id = self._runner.session_id
        if session_id is None:
            self._write_ui_entry("error", "subagents.session_required")
            return
        try:
            relationships = await controller.list_subagent_relationships(
                ListSubagentRelationshipsRequest(session_id),
            )
        except Exception as error:
            self._write_entry("error", f"{type(error).__name__}: {error}")
            return
        if not relationships:
            self._write_ui_entry("status", "subagents.none")
            return

        lines = [
            ui_text(
                self._language,
                "subagents.summary",
                task_id=relationship.parent_task_id,
                child_session_id=relationship.child_session_id,
                provider=relationship.child_provider,
                model=relationship.child_model,
                status=ui_text(
                    self._language,
                    f"tasks.status.{relationship.task_status.value}",
                ),
                created=relationship.created_at.astimezone().strftime("%H:%M:%S"),
                updated=relationship.child_updated_at.astimezone().strftime("%H:%M:%S"),
                actions=(
                    ", ".join(action.value for action in relationship.available_actions)
                    or ui_text(self._language, "subagents.actions.none")
                ),
            )
            for relationship in relationships
        ]
        self._write_ui_entry(
            "system",
            "subagents.heading",
            lines="\n".join(lines),
        )

    async def _run_subagent_relationship_action(self, arguments: str) -> None:
        """Run one explicit relationship action through the application owner.

        通过应用 owner 执行一次明确的关系生命周期动作.

        The TUI only parses the small command shape and projects the bounded
        result.  It does not touch SQLite or infer ownership from a child ID.
        TUI 只解析精简命令形状并投影有界结果,不会直接访问 SQLite 或仅凭子会话 ID 推断归属.
        """

        controller = self._subagent_relationship_lifecycle
        if controller is None:
            self._write_ui_entry("error", "subagents.actions_unavailable")
            return
        parent_session_id = self._runner.session_id
        if parent_session_id is None:
            self._write_ui_entry("error", "subagents.session_required")
            return
        if self._turn_worker is not None and self._turn_worker.is_running:
            self._write_ui_entry("error", "session.resume_running")
            return

        action_text, separator, task_id = arguments.partition(" ")
        if not separator or not action_text.strip() or not task_id.strip():
            self._write_ui_entry("error", "subagents.actions_usage")
            return
        try:
            action = SubagentRelationshipAction(action_text.casefold())
        except ValueError:
            self._write_ui_entry("error", "subagents.actions_usage")
            return
        try:
            result = await controller.execute(
                SubagentRelationshipActionRequest(
                    parent_session_id=parent_session_id,
                    parent_task_id=task_id.strip(),
                    action=action,
                )
            )
        except Exception as error:
            self._write_entry("error", f"{type(error).__name__}: {error}")
            return

        if result.action is SubagentRelationshipAction.RESUME:
            await self._apply_session_selection(result.child_session_id)
        elif result.action is SubagentRelationshipAction.FORK:
            assert result.forked_session_id is not None
            self._write_ui_entry(
                "status",
                "subagents.actions.forked",
                session_id=result.forked_session_id,
            )
        else:
            self._write_ui_entry(
                "status",
                "subagents.actions.deleted",
                session_id=result.child_session_id,
            )

    async def _poll_background_tasks(self) -> None:
        if self._task_controller is None or self._task_polling:
            return
        self._task_polling = True
        try:
            await self._ensure_background_wake_state()
            if not self._background_wake_state_loaded:
                return
            snapshots = await self._task_controller.list_background_tasks()
        except Exception:
            return
        finally:
            self._task_polling = False

        pending_completion_ids = {
            snapshot.task_id
            for snapshot in snapshots
            if snapshot.status.terminal and not snapshot.completion_reported
        }
        reconciled = self._background_wake_state.reconcile_visible_tasks(pending_completion_ids)
        if reconciled != self._background_wake_state:
            self._background_wake_state = reconciled
            self._pending_auto_wake_tasks.intersection_update(
                self._background_wake_state.pending_task_ids
            )

        for snapshot in snapshots:
            if not snapshot.status.terminal:
                continue
            if snapshot.task_id not in self._announced_terminal_tasks:
                self._announced_terminal_tasks.add(snapshot.task_id)
                category = (
                    "status"
                    if snapshot.status
                    in {BackgroundTaskStatus.COMPLETED, BackgroundTaskStatus.CANCELLED}
                    else "error"
                )
                self._write_entry(category, self._task_completion_message(snapshot))
                self._background_wake_state = self._background_wake_state.record_terminal_task(
                    snapshot.task_id,
                    enqueue=(
                        self._background_task_wake_policy is BackgroundTaskWakePolicy.ENABLED
                        and not snapshot.completion_reported
                    ),
                )
            if snapshot.task_id in self._background_wake_state.pending_task_ids:
                self._pending_auto_wake_tasks.add(snapshot.task_id)

        await self._persist_background_wake_state()

        if (
            self._background_task_wake_policy is BackgroundTaskWakePolicy.ENABLED
            and self._pending_auto_wake_tasks
            and not (self._turn_worker is not None and self._turn_worker.is_running)
        ):
            await self._start_background_wake()

    async def _ensure_background_wake_state(self) -> None:
        if self._background_wake_state_loaded:
            return
        controller = self._task_controller
        if controller is None:
            self._background_wake_state_loaded = True
            return
        try:
            state = await controller.load_background_wake_state()
        except Exception:
            self._background_wake_state = BackgroundWakeState()
            self._background_wake_state_loaded = True
            return
        recovered = state.recover_after_restart()
        self._background_wake_state = recovered
        self._announced_terminal_tasks = set(recovered.announced_task_ids)
        self._pending_auto_wake_tasks.clear()
        self._background_wake_state_loaded = True
        if recovered != state:
            await self._persist_background_wake_state()

    async def _persist_background_wake_state(self) -> None:
        controller = self._task_controller
        if controller is None or not self._background_wake_state_loaded:
            return
        try:
            await controller.save_background_wake_state(self._background_wake_state)
        except Exception:
            # Wake bookkeeping must never make a task poll or user turn fail.
            return

    def _reset_background_task_tracking(self) -> None:
        self._announced_terminal_tasks.clear()
        self._pending_auto_wake_tasks.clear()
        self._background_wake_state = BackgroundWakeState()
        self._background_wake_state_loaded = self._task_controller is None
        self._background_wake_active = False
        self._background_wake_task_ids = ()

    async def _start_background_wake(self) -> None:
        if self._turn_worker is not None and self._turn_worker.is_running:
            return
        if not self._pending_auto_wake_tasks:
            return
        now = datetime.now(UTC)
        decision = self._background_wake_state.decision(
            now,
            limits=self._background_wake_limits,
        )
        if decision is not BackgroundWakeDecision.ALLOW:
            return
        self._background_wake_state = self._background_wake_state.begin_wake(
            now,
            limits=self._background_wake_limits,
        )
        await self._persist_background_wake_state()
        self._pending_auto_wake_tasks.clear()
        self._background_wake_active = True
        self._background_wake_task_ids = ()
        self._assistant_parts.clear()
        self._first_token_seen = False
        self._reasoning_announced = False
        self._turn_completion = None
        self._terminal_execution_status = None
        self._terminal_execution_recoverable = False
        self._finalizing = False
        self._turn_usage_reported = False
        self._begin_pending_assistant()
        self._write_ui_entry("status", "background_wake.started")
        self._turn_worker = self.run_worker(
            self._run_background_wake(),
            name="background-auto-wake",
            group="agent",
            exclusive=True,
            exit_on_error=False,
        )

    async def _complete_background_wake(self) -> None:
        """Commit wake consumption only after the model turn completed successfully.

        仅在模型回合成功完成后提交唤醒消费."""

        task_ids = self._background_wake_task_ids
        if not task_ids:
            self._background_wake_state = self._background_wake_state.abandon_wake(
                failed_at=datetime.now(UTC)
            )
        else:
            self._background_wake_state = self._background_wake_state.complete_wake(
                task_ids,
                completed_at=datetime.now(UTC),
            )
            self._pending_auto_wake_tasks.difference_update(task_ids)
        self._background_wake_active = False
        self._background_wake_task_ids = ()
        await self._persist_background_wake_state()

    def _task_summary(self, snapshot: BackgroundTaskSnapshot) -> str:
        exit_note = (
            ui_text(self._language, "tasks.exit", code=snapshot.exit_code)
            if snapshot.exit_code is not None
            else ""
        )
        truncation_note = ui_text(self._language, "tasks.truncated") if snapshot.truncated else ""
        started = snapshot.started_at.astimezone().strftime("%H:%M:%S")
        return ui_text(
            self._language,
            "tasks.summary",
            task_id=snapshot.task_id,
            status=ui_text(self._language, f"tasks.status.{snapshot.status.value}"),
            exit_note=exit_note,
            bytes=snapshot.total_output_bytes,
            truncated=truncation_note,
            started=started,
        )

    def _task_completion_message(self, snapshot: BackgroundTaskSnapshot) -> str:
        exit_note = (
            ui_text(self._language, "tasks.completion.exit", code=snapshot.exit_code)
            if snapshot.exit_code is not None
            else ""
        )
        return ui_text(
            self._language,
            f"tasks.completion.{snapshot.status.value}",
            task_id=snapshot.task_id,
            exit_note=exit_note,
        )

    def _session_task_summary(self, task: SessionTask) -> str:
        started = task.started_at.astimezone().strftime("%H:%M:%S")
        finished = (
            ui_text(
                self._language,
                "tasks.session.finished",
                finished=task.finished_at.astimezone().strftime("%H:%M:%S"),
            )
            if task.finished_at is not None
            else ""
        )
        plan_note = ""
        if task.plan_snapshot is not None:
            completed = sum(step.status.value == "completed" for step in task.plan_snapshot.steps)
            plan_note = ui_text(
                self._language,
                "tasks.session.plan_revision",
                fingerprint=task.plan_snapshot.fingerprint[:12],
                completed=completed,
                total=len(task.plan_snapshot.steps),
            )
        return ui_text(
            self._language,
            "tasks.session.summary",
            task_id=task.task_id,
            kind=ui_text(self._language, f"tasks.kind.{task.kind.value}"),
            status=ui_text(self._language, f"tasks.status.{task.status.value}"),
            started=started,
            finished=finished,
            plan=plan_note,
        )

    def _stopped_task_note(self, count: int) -> str:
        if count == 0:
            return ""
        if count == 1:
            return ui_text(self._language, "tasks.stopped_one")
        return ui_text(self._language, "tasks.stopped_many", count=count)

    def _session_execution_record(self) -> SessionExecutionRecord | None:
        controller = self._session_controller
        if controller is None:
            return None
        record = getattr(controller, "execution_record", None)
        return record if isinstance(record, SessionExecutionRecord) else None

    def _session_selection_owner(self) -> SessionController | None:
        """Return the narrow session-selection boundary used by the TUI.

        返回 TUI 使用的窄会话选择边界.

        ``session_controller`` remains an optional compatibility input because
        it also supplies the current execution-record projection to the TUI.
        Production bootstrap injects the narrower application service for
        selection operations while retaining that projection compatibility.

        ``session_controller`` 仍是可选兼容输入,因为它还向 TUI 提供当前执行记录投影.
        生产 bootstrap 为选择操作注入更窄的应用服务,同时保留该投影兼容性.
        """

        return self._session_selection_service or self._session_controller

    def _write_recoverable_resume_notice(
        self,
        record: SessionExecutionRecord | None,
    ) -> None:
        if record is None or not record.outcome.recoverable:
            return
        key_by_status = {
            AgentExecutionStatus.STUCK: "session.stuck_recoverable",
            AgentExecutionStatus.BUDGET_LIMITED: "session.budget_limited_recoverable",
        }
        key = key_by_status.get(record.outcome.status)
        if key is not None:
            self._write_ui_entry("recoverable", key)

    def _replace_transcript(self, items: Sequence[SessionItem]) -> None:
        transcript = self.query_one("#transcript", VerticalScroll)
        transcript.remove_children()
        self._entries.clear()
        self._entry_widgets.clear()
        self._tool_feedback_by_call.clear()
        self._tool_feedback_by_entry.clear()
        self._plan_entry_index = None
        self._plan_comments = ()
        self._pending_assistant = None
        self._assistant_parts.clear()
        for item in items:
            if not isinstance(item, Message) or item.role is Role.SYSTEM:
                continue
            if item.role is Role.TOOL:
                self._write_ui_entry(
                    "tool",
                    "restore.result",
                    name=item.name or ui_text(self._language, "value.unknown"),
                )
                continue
            content = self._bounded_restored_text(item.model_content())
            if content:
                category = "user" if item.role is Role.USER else "assistant"
                self._write_entry(category, content)
            if item.role is Role.ASSISTANT and item.tool_calls:
                names = ", ".join(call.name for call in item.tool_calls)
                self._write_ui_entry("tool", "restore.request", names=names)
        if self._plan is not None:
            self._upsert_plan_entry(self._plan, self._plan_comments)

    def _bounded_restored_text(self, content: str) -> str:
        if len(content) <= _RESTORED_MESSAGE_LIMIT:
            return content
        return (
            f"{content[:_RESTORED_MESSAGE_LIMIT]}\n{ui_text(self._language, 'restore.truncated')}"
        )

    @staticmethod
    def _semantic_value_style(name: str, value: object) -> str | None:
        if name in {"provider", "model", "profile", "source"}:
            return f"bold {ACCENT_CODE}"
        if name in {"name", "task_id", "session_id", "title"}:
            return f"bold {ACCENT_CODE}"
        if name == "path":
            return ACCENT_CODE
        if name == "cwd":
            return TEXT_SECONDARY
        if name in {"effect", "outcome", "status"}:
            return f"bold {ACCENT_SUCCESS}"
        if name in {"duration", "steps", "step"}:
            return f"bold {TEXT_SECONDARY}"
        if name == "context":
            return f"bold {TEXT_SECONDARY}"
        if name in {"effort", "requested", "effective"}:
            try:
                effort = ReasoningEffort(str(value))
            except ValueError:
                return f"bold {TEXT_EMPHASIS}"
            return f"bold {EFFORT_STYLES[effort.value]}"
        if name == "mode":
            try:
                mode = InteractionMode(str(value))
            except ValueError:
                return f"bold {TEXT_EMPHASIS}"
            return f"bold {MODE_STYLES[mode.value]}"
        if name == "policy":
            return TEXT_SECONDARY
        if name in {"message", "reason", "error"}:
            return ERROR_TEXT_STYLE
        return None

    def _render_entry(
        self,
        category: str,
        content: str,
        *,
        ui_key: str | None = None,
        ui_values: tuple[tuple[str, object], ...] = (),
    ) -> RenderableType:
        if category == "user":
            return Text(content, style=USER_TEXT_STYLE, overflow="fold")
        if category == "assistant":
            return AssistantMarkdown(
                content,
                code_theme=_markdown_code_theme(),
                style=ASSISTANT_TEXT_STYLE,
                hyperlinks=False,
            )

        labels = {
            "error": (f"{_ERROR_MARK} {ui_text(self._language, 'label.error')}", ERROR_LABEL_STYLE),
            "recoverable": (
                f"! {ui_text(self._language, 'label.status')}",
                RECOVERABLE_LABEL_STYLE,
            ),
            "status": (f"… {ui_text(self._language, 'label.status')}", STATUS_LABEL_STYLE),
            "system": ("NEURO", SYSTEM_LABEL_STYLE),
            "tool": (f"• {ui_text(self._language, 'label.tool')}", TOOL_LABEL_STYLE),
        }
        body_styles = {
            "error": ERROR_DETAIL_STYLE,
            "recoverable": RECOVERABLE_TEXT_STYLE,
            "status": STATUS_TEXT_STYLE,
            "system": SYSTEM_TEXT_STYLE,
            "tool": TOOL_TEXT_STYLE,
        }
        if category == "plan" and self._plan is not None:
            return self._render_plan(self._plan, self._plan_comments)
        label, label_style = labels.get(category, (category.title(), f"bold {TEXT_PRIMARY}"))
        body = Text(content, style=body_styles.get(category, TEXT_BODY), overflow="fold")
        for name, value in ui_values:
            style = self._semantic_value_style(name, value)
            rendered_value = str(value)
            if style is not None and rendered_value:
                body.highlight_words((rendered_value,), style=style, case_sensitive=True)

        table = Table.grid(expand=True, padding=(0, 1))
        table.add_column(width=10, justify="right", no_wrap=True)
        table.add_column(ratio=1, overflow="fold")
        table.add_row(Text(label, style=label_style), body)
        return table

    def _render_tool_feedback(
        self,
        state: ToolFeedbackState,
        *,
        body: Text | None = None,
    ) -> RenderableType:
        table = Table.grid(expand=True, padding=(0, 1))
        table.add_column(width=10, justify="right", no_wrap=True)
        table.add_column(ratio=1, overflow="fold")
        label_style = ERROR_LABEL_STYLE if state.is_error else TOOL_LABEL_STYLE
        table.add_row(
            Text(ui_text(self._language, "label.tool"), style=label_style),
            body if body is not None else self._tool_feedback_body(state),
        )
        return table

    def _write_ui_entry(self, category: str, key: str, **values: object) -> None:
        self._write_entry(
            category,
            ui_text(self._language, key, **values),
            ui_key=key,
            ui_values=tuple(values.items()),
        )

    def _write_entry(
        self,
        category: str,
        content: str,
        *,
        ui_key: str | None = None,
        ui_values: tuple[tuple[str, object], ...] = (),
        tool_state: ToolFeedbackState | None = None,
    ) -> None:
        entry = TranscriptEntry(category, content, ui_key, ui_values)
        if tool_state is not None:
            tool_widget = ToolFeedbackMessage(
                self._render_tool_feedback(tool_state),
                entry_index=tool_state.entry_index,
            )
            self._configure_tool_feedback_widget(tool_widget, tool_state)
            widget: ConversationMessage = tool_widget
        else:
            widget = ConversationMessage(
                category,
                self._render_entry(
                    category,
                    content,
                    ui_key=ui_key,
                    ui_values=ui_values,
                ),
            )
        transcript = self.query_one("#transcript", VerticalScroll)
        follow = transcript.is_vertical_scroll_end
        pending = self._pending_assistant
        if pending is not None and pending.parent is transcript:
            transcript.mount(widget, before=pending)
        else:
            transcript.mount(widget)
        self._entries.append(entry)
        self._entry_widgets.append(widget)
        if follow:
            transcript.scroll_end(animate=False)

    def _begin_pending_assistant(self) -> None:
        if self._pending_assistant is not None:
            return
        self._start_model_loading()
        pending = ConversationMessage(
            "assistant",
            self._render_model_loading(),
            pending=True,
        )
        self._pending_assistant = pending
        transcript = self.query_one("#transcript", VerticalScroll)
        transcript.mount(pending)
        transcript.scroll_end(animate=False)

    def _update_pending_assistant(self, content: str) -> None:
        if self._pending_assistant is None:
            self._begin_pending_assistant()
        pending = self._pending_assistant
        assert pending is not None
        transcript = self.query_one("#transcript", VerticalScroll)
        follow = transcript.is_vertical_scroll_end
        pending.set_pending(False)
        pending.update(self._render_entry("assistant", content))
        if follow:
            transcript.scroll_end(animate=False)

    def _finish_pending_assistant(self, content: str) -> None:
        if self._pending_assistant is None:
            self._begin_pending_assistant()
        pending = self._pending_assistant
        assert pending is not None
        transcript = self.query_one("#transcript", VerticalScroll)
        follow = transcript.is_vertical_scroll_end
        pending.set_pending(False)
        pending.update(self._render_entry("assistant", content))
        self._entries.append(TranscriptEntry("assistant", content))
        self._entry_widgets.append(pending)
        self._pending_assistant = None
        self._stop_model_loading()
        if follow:
            transcript.scroll_end(animate=False)

    async def _discard_pending_assistant(self) -> None:
        pending = self._pending_assistant
        self._pending_assistant = None
        self._assistant_parts.clear()
        self._stop_model_loading()
        if pending is not None and pending.parent is not None:
            await pending.remove()

    def _start_model_loading(self) -> None:
        self._model_loading = True
        self._loading_animation.reset()
        self._loading_animation_elapsed = 0.0

    def _stop_model_loading(self) -> None:
        if not self._model_loading:
            return
        self._model_loading = False
        self._loading_animation_elapsed = 0.0

    def _advance_model_loading_animation(self) -> None:
        if self._model_loading:
            self._loading_animation_elapsed += _LOADING_ANIMATION_TICK_SECONDS
            if self._loading_animation_elapsed + 1e-9 >= self._loading_animation.delay_seconds:
                self._loading_animation_elapsed = 0.0
                self._loading_animation.advance()
                pending = self._pending_assistant
                if pending is not None and not self._assistant_parts and pending.parent is not None:
                    pending.update(self._render_model_loading())

    def _refresh_running_tool_elapsed(self) -> None:
        for state in tuple(self._tool_feedback_by_entry.values()):
            if state.phase == "running":
                self._refresh_tool_feedback(state)

    def _apply_language_to_chrome(self) -> None:
        self.sub_title = ui_text(self._language, "subtitle")
        self.query_one("#prompt", Input).placeholder = ui_text(
            self._language,
            "prompt.placeholder",
        )
        shortcuts = Text(ui_text(self._language, "shortcuts"), style=TEXT_MUTED)
        shortcuts.highlight_regex(r"\^(?:[A-Z]|,)|Shift\+Tab", style=f"bold {TEXT_EMPHASIS}")
        self.query_one("#shortcut-bar", Static).update(shortcuts)
        prompt = self.query_one("#prompt", Input)
        self._refresh_command_hints(prompt.value)
        self._refresh_runtime_bar()

    def _slash_completions(self, value: str) -> tuple[SlashCompletion, ...]:
        provider_names = (
            tuple(option.name for option in self._provider_controller.profiles if option.selectable)
            if self._provider_controller is not None
            else ()
        )
        return slash_completions(value, provider_names=provider_names)

    def _refresh_command_hints(self, value: str) -> None:
        widget = self.query_one("#command-hints", Static)
        completions = self._slash_completions(value)
        if not completions:
            widget.update("")
            widget.display = False
            return

        hints = Text()
        hints.append(ui_text(self._language, "command_hint.tab"), style=f"bold {TEXT_EMPHASIS}")
        hints.append("  ", style=TEXT_DISABLED)
        for index, completion in enumerate(completions[:_COMMAND_HINT_LIMIT]):
            if index:
                hints.append("  ·  ", style=TEXT_DISABLED)
            hints.append(completion.display, style=TEXT_SECONDARY)
        if len(completions) > _COMMAND_HINT_LIMIT:
            hints.append("  ·  …", style=TEXT_MUTED)
        widget.update(hints)
        widget.display = True

    def _context_percentage(self) -> str:
        window = self._context_window_tokens
        if window is None:
            return self._context_token_usage()
        percentage = self._context_used_tokens / window * 100
        rendered = "<0.1%" if 0 < percentage < 0.1 else f"{percentage:.1f}%"
        return f"~{rendered}" if self._context_usage_estimated else rendered

    def _context_color(self) -> str:
        window = self._context_window_tokens
        if window is None:
            return TEXT_SECONDARY
        ratio = self._context_used_tokens / window
        if ratio >= 0.8:
            return ACCENT_WARNING
        return TEXT_SECONDARY

    def _context_token_usage(self) -> str:
        tokens = self._context_used_tokens
        if tokens >= 1_000_000:
            rendered = f"{tokens / 1_000_000:.1f}M"
        elif tokens >= 1_000:
            rendered = f"{tokens / 1_000:.1f}k"
        else:
            rendered = f"{tokens:,}"
        approximation = "≈" if self._context_usage_estimated else ""
        return f"{approximation}{rendered} tok"

    def _context_usage_summary(self) -> str:
        window = self._context_window_tokens
        if window is None:
            return self._context_token_usage()
        approximation = "≈" if self._context_usage_estimated else ""
        return (
            f"{self._context_percentage()} "
            f"({approximation}{self._context_used_tokens:,}/{window:,})"
        )

    def _loading_wave(self) -> Text:
        symbols = ("▁", "▂", "▃", "▄", "▅", "▆", "▇", "█")
        wave = Text()
        for level in self._loading_animation.levels():
            safe_level = max(0, min(7, level))
            wave.append(symbols[safe_level], style=loading_style(safe_level))
        return wave

    def _render_model_loading(self) -> Text:
        loading = self._loading_wave()
        loading.append("  ")
        key = "turn.finalizing" if self._finalizing else "turn.waiting"
        loading.append(ui_text(self._language, key), style=WAITING_STYLE)
        loading.append("  ·  ↓", style=TEXT_DIM)
        loading.append(self._context_token_usage(), style=self._context_color())
        return loading

    def _refresh_runtime_bar(self) -> None:
        model = Text()
        model.append(
            f"{ui_text(self._language, 'runtime.model')}  ",
            style=f"bold {TEXT_DIM}",
        )
        model.append(self._provider_name, style=f"bold {ACCENT_CODE}")
        if self._model_name != self._provider_name:
            model.append(" · ", style=TEXT_DIM)
            model.append(self._model_name, style=f"bold {ACCENT_CODE}")
        self.query_one("#runtime-model", Static).update(model)

        workspace = Text(justify="right", overflow="ellipsis", no_wrap=True)
        workspace.append(
            f"{ui_text(self._language, 'runtime.workspace')}  ",
            style=f"bold {TEXT_DIM}",
        )
        workspace.append(self._display_cwd(), style=TEXT_DIM)
        workspace_widget = self.query_one("#runtime-workspace", Static)
        workspace_widget.update(workspace)
        workspace_widget.tooltip = str(self._cwd)

        context = Text()
        context.append(
            f"{ui_text(self._language, 'runtime.context')}  ",
            style=f"bold {TEXT_DIM}",
        )
        context.append(
            self._context_percentage(),
            style=f"bold {self._context_color()}",
        )
        context_widget = self.query_one("#runtime-context", Static)
        context_widget.update(context)
        if self._context_window_tokens is None:
            context_widget.tooltip = self._context_token_usage()
        else:
            context_widget.tooltip = ui_text(
                self._language,
                (
                    "runtime.context_help_estimated"
                    if self._context_usage_estimated
                    else "runtime.context_help_reported"
                ),
                used=f"{self._context_used_tokens:,}",
                window=f"{self._context_window_tokens:,}",
            )

        requested = self._reasoning_effort
        effective = self._effective_reasoning_effort
        effort = Text()
        effort.append(
            f"{ui_text(self._language, 'runtime.effort')}  ",
            style=f"bold {TEXT_DIM}",
        )
        effort.append(
            f"{requested.glyph} {requested.value}",
            style=f"bold {EFFORT_STYLES[requested.value]}",
        )
        if effective is not requested:
            effort.append(" → ", style=TEXT_DIM)
            effort.append(
                f"{effective.glyph} {effective.value}",
                style=f"bold {EFFORT_STYLES[effective.value]}",
            )
        effort_widget = self.query_one("#runtime-effort", Static)
        effort_widget.update(effort)
        effort_widget.tooltip = ui_text(self._language, "runtime.effort_help")

        mode = Text()
        mode.append(
            f"{ui_text(self._language, 'runtime.mode')}  ",
            style=f"bold {TEXT_DIM}",
        )
        mode.append(
            f"{self._interaction_mode.glyph} {self._interaction_mode.value}",
            style=f"bold {MODE_STYLES[self._interaction_mode.value]}",
        )
        mode_widget = self.query_one("#runtime-mode", Static)
        mode_widget.update(mode)
        mode_widget.tooltip = ui_text(
            self._language,
            (
                "runtime.mode_help_auto_unrestricted"
                if self._interaction_mode is InteractionMode.AUTO and self._auto_mode_unrestricted
                else f"runtime.mode_help.{self._interaction_mode.value}"
            ),
        )

    def _display_cwd(self) -> str:
        try:
            relative = self._cwd.resolve().relative_to(Path.home().resolve())
        except (OSError, RuntimeError, ValueError):
            return str(self._cwd)
        return "~" if str(relative) == "." else f"~/{relative}"

    def _reasoning_effort_summary(self) -> str:
        requested = self._reasoning_effort
        effective = self._effective_reasoning_effort
        summary = f"{requested.glyph} {requested.value}"
        if effective is not requested:
            summary += f" → {effective.glyph} {effective.value}"
        return summary

    def _interaction_mode_summary(self) -> str:
        summary = f"{self._interaction_mode.glyph} {self._interaction_mode.value}"
        if self._interaction_mode is InteractionMode.AUTO and not self._auto_mode_unrestricted:
            summary += f" ({ui_text(self._language, 'mode.limited')})"
        return summary

    def _refresh_localized_interface(self) -> None:
        self._apply_language_to_chrome()
        for index, (entry, widget) in enumerate(
            zip(self._entries, self._entry_widgets, strict=True)
        ):
            if entry.category == "plan" and self._plan is not None:
                rendered_plan = self._render_plan(self._plan, self._plan_comments)
                self._entries[index] = TranscriptEntry("plan", rendered_plan.plain)
                widget.update(rendered_plan)
                continue
            tool_state = self._tool_feedback_by_entry.get(index)
            if tool_state is not None:
                body = self._tool_feedback_body(tool_state)
                self._entries[index] = TranscriptEntry("tool", body.plain)
                widget.update(self._render_tool_feedback(tool_state, body=body))
                if isinstance(widget, ToolFeedbackMessage):
                    self._configure_tool_feedback_widget(widget, tool_state)
                continue
            if entry.ui_key is not None:
                content = ui_text(
                    self._language,
                    entry.ui_key,
                    **dict(entry.ui_values),
                )
                entry = replace(entry, text=content)
                self._entries[index] = entry
            widget.update(
                self._render_entry(
                    entry.category,
                    entry.text,
                    ui_key=entry.ui_key,
                    ui_values=entry.ui_values,
                )
            )
        if self._pending_assistant is not None:
            if self._assistant_parts:
                self._pending_assistant.update(
                    self._render_entry("assistant", "".join(self._assistant_parts))
                )
            else:
                self._pending_assistant.update(self._render_model_loading())


__all__ = [
    "TUI_RELOAD_PROVIDER_SETTINGS",
    "ApprovalController",
    "AssistantMarkdown",
    "BackgroundWakeSettingsScreen",
    "ConversationMessage",
    "ConversationRunner",
    "LanguageSettingsScreen",
    "NetworkProxySettingsScreen",
    "NeuroCodeApp",
    "PermissionApprovalScreen",
    "ProviderController",
    "ProviderSelectionScreen",
    "ProviderSettingsScreen",
    "ProviderSettingsSubmission",
    "ProviderSetupApp",
    "ReasoningController",
    "ReasoningEffortScreen",
    "SessionController",
    "SessionSelectionScreen",
    "SessionTaskController",
    "SettingsScreen",
    "TaskController",
    "ToolFeedbackMessage",
    "TranscriptEntry",
]
