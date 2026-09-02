from __future__ import annotations

import os
import sys
from collections import deque
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar, TypeVar

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.geometry import Size
from textual.widget import Widget
from textual.widgets import Static
from textual.worker import Worker

from neuro_code.application.permissions.contracts import PermissionApproval, PermissionRequest
from neuro_code.application.ports.provider_catalog import (
    ProviderCatalog,
)
from neuro_code.application.ports.provider_settings import (
    ManagedProviderSettings,
    ProviderSettingsStore,
)
from neuro_code.application.ports.ui_preferences import UiPreferencesStore
from neuro_code.application.sessions.selection import (
    SessionSelectionService,
)
from neuro_code.application.sessions.subagent_lifecycle import (
    SubagentRelationshipLifecycleController,
)
from neuro_code.application.sessions.subagent_queries import (
    SubagentRelationshipQueryController,
)
from neuro_code.application.sessions.turns import SessionTurnService
from neuro_code.application.tools.service import (
    SessionToolOutputArtifactApplicationService,
)
from neuro_code.application.workflows.plan_execution import (
    PlanExecutionService,
)
from neuro_code.application.workflows.plan_scheduling import (
    PlanSchedulingService,
)
from neuro_code.application.workflows.session_task_execution import (
    QueuedPlanExecutionService,
)
from neuro_code.application.workflows.subagent import (
    ReadOnlySubagentApplicationService,
)
from neuro_code.application.workflows.subagent_capabilities import SubagentCapabilitySet
from neuro_code.domain.background_tasks.models import (
    BackgroundTaskWakePolicy,
    BackgroundWakeLimits,
    BackgroundWakeState,
)
from neuro_code.domain.conversation.context import estimate_context_tokens
from neuro_code.domain.conversation.interaction_mode import InteractionMode
from neuro_code.domain.conversation.messages import SessionItem
from neuro_code.domain.conversation.reasoning import ReasoningEffort
from neuro_code.domain.execution import (
    SessionExecutionRecord,
)
from neuro_code.domain.plans import PlanComment
from neuro_code.interfaces.tui.clipboard import (
    ClipboardWriter,
    ClipboardWriteResult,
    SystemClipboardWriter,
)
from neuro_code.interfaces.tui.contracts import (
    ApprovalController,
    ConversationRunner,
    InteractionModeController,
    PlanController,
    ProviderController,
    ReasoningController,
    SessionController,
    SessionTaskController,
    TaskController,
)
from neuro_code.interfaces.tui.controllers.background import BackgroundControllerMixin
from neuro_code.interfaces.tui.controllers.commands import CommandControllerMixin
from neuro_code.interfaces.tui.controllers.plans import PlanControllerMixin
from neuro_code.interfaces.tui.controllers.preferences import PreferencesControllerMixin
from neuro_code.interfaces.tui.controllers.provider import ProviderControllerMixin
from neuro_code.interfaces.tui.controllers.runtime import RuntimeControllerMixin
from neuro_code.interfaces.tui.controllers.session import SessionControllerMixin
from neuro_code.interfaces.tui.controllers.tasks import TaskControllerMixin
from neuro_code.interfaces.tui.controllers.tool_activity.events import ToolActivityEventsMixin
from neuro_code.interfaces.tui.controllers.tool_activity.inspector import (
    ToolActivityInspectorMixin,
)
from neuro_code.interfaces.tui.controllers.tool_activity.presentation import (
    ToolActivityPresentationMixin,
)
from neuro_code.interfaces.tui.controllers.transcript import TranscriptControllerMixin
from neuro_code.interfaces.tui.controllers.turns import TurnControllerMixin
from neuro_code.interfaces.tui.interaction import TuiUserInteraction
from neuro_code.interfaces.tui.screens import PermissionApprovalScreen
from neuro_code.interfaces.tui.state import (
    _DEFAULT_BACKGROUND_WAKE_LIMITS,
    _LOADING_ANIMATION_TICK_SECONDS,
    _PROMPT_MARK,
    _TASK_POLL_SECONDS,
    _TERMINAL_SIZE_POLL_SECONDS,
    _TOOL_ELAPSED_UPDATE_SECONDS,
    CollapsingPulseAnimation,
    ToolActivityGroupState,
    ToolFeedbackState,
    TranscriptEntry,
    _ActiveToolInspector,
)
from neuro_code.interfaces.tui.text import ui_text
from neuro_code.interfaces.tui.theme import (
    BRAND_TEXT,
    MARKDOWN_THEME,
    TEXT_MUTED,
    TEXTUAL_THEME,
)
from neuro_code.interfaces.tui.widgets import ConversationMessage, PromptInput
from neuro_code.shared.ui_language import UiLanguage


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


_WidgetType = TypeVar("_WidgetType", bound=Widget)


class NeuroCodeApp(
    TurnControllerMixin,
    ToolActivityEventsMixin,
    ToolActivityInspectorMixin,
    ToolActivityPresentationMixin,
    CommandControllerMixin,
    PreferencesControllerMixin,
    ProviderControllerMixin,
    SessionControllerMixin,
    PlanControllerMixin,
    TaskControllerMixin,
    BackgroundControllerMixin,
    TranscriptControllerMixin,
    RuntimeControllerMixin,
    App[None],
):
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
        text-style: none;
    }

    Button:hover {
        background: $surface-hover;
    }

    Button:focus {
        background: $surface;
        color: $text-primary;
        border-left: tall $border-focus;
        text-style: none;
    }

    Button.-primary,
    Button.-success,
    Button.-warning,
    Button.-error {
        background: $surface;
        border: none;
    }

    Button.-success {
        color: $success;
    }

    Button.-warning {
        color: $warning;
    }

    Button.-error {
        color: $error;
    }

    MenuOptionButton,
    MenuOptionButton:hover,
    MenuOptionButton:focus {
        background: $surface;
        border: none;
        text-style: none;
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

    .modal-dialog {
        padding: $space-2 $space-3;
        background: $surface;
        border: solid $border;
    }

    .modal-s {
        width: 76%;
        max-width: 72;
    }

    .modal-m {
        width: 82%;
        max-width: 88;
    }

    .modal-l {
        width: 92%;
        max-width: 116;
    }

    #header {
        height: 3;
        padding: $space-1 $space-4 $space-0 $space-4;
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
        padding: $space-2 $space-4;
        background: $background;
        color: $text-body;
    }

    .conversation-message {
        width: 100%;
        max-width: 116;
        height: auto;
        min-height: 1;
        margin-bottom: $space-1;
        padding: $space-0 $space-1;
        color: $text-body;
    }

    .message-user {
        margin-bottom: $space-2;
        background: $surface;
        color: $text-primary;
        border-left: solid $border;
        text-style: bold;
    }

    .message-assistant {
        margin-bottom: $space-2;
        background: $background;
        color: $text-body;
    }

    .message-pending {
        color: $text-secondary;
        text-style: italic;
    }

    .message-system {
        color: $text-emphasis;
    }

    .message-tool {
        margin-bottom: $space-2;
        color: $text-body;
    }

    .message-tool.tool-interactive:hover,
    .message-tool.tool-interactive:focus {
        background: $background;
        border-left: solid $border-focus;
    }

    .message-tool.tool-peek {
        height: 12;
        max-height: 12;
        overflow-y: hidden;
    }

    .message-status {
        color: $text-secondary;
    }

    .message-recoverable {
        color: $text-emphasis;
        border-left: solid $border;
    }

    .message-error {
        color: $text-primary;
        border-left: tall $error;
        text-style: bold;
    }

    #composer {
        height: auto;
        padding: $space-0 $space-4 $space-1 $space-4;
        background: $background;
    }

    #turn-activity {
        display: none;
        width: 100%;
        max-width: 116;
        height: 1;
        padding: $space-0 $space-1;
        background: $background;
        color: $text-secondary;
        overflow: hidden hidden;
    }

    #runtime-bar {
        width: 100%;
        height: 1;
        padding: $space-0 $space-1;
        background: $background;
        color: $text-secondary;
        align-vertical: middle;
    }

    #runtime-primary,
    #runtime-secondary {
        width: 1fr;
        height: 1;
        overflow: hidden hidden;
    }

    #runtime-secondary {
        text-align: right;
    }

    #prompt-row {
        height: auto;
        min-height: 3;
        max-height: 10;
        padding: $space-0 $space-1;
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
        max-height: 8;
        padding: 0;
        margin: 0;
        border: none;
        background: $surface;
        color: $text-primary;
        scrollbar-size-vertical: 1;
    }

    #prompt > .text-area--cursor-line {
        background: $surface;
    }

    #prompt > .text-area--selection {
        background: $surface-selected;
    }

    #prompt > .text-area--cursor {
        color: $surface;
        background: $text-muted;
    }

    #prompt-row:focus-within {
        border-left: tall $border-focus;
    }

    #command-hints {
        display: none;
        width: 100%;
        height: auto;
        max-height: 3;
        padding: $space-0 $space-1;
        background: $background;
        color: $text-secondary;
        overflow: hidden hidden;
    }

    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+c", "cancel_turn", "Cancel", priority=True, show=False),
        Binding("ctrl+shift+c", "copy_prompt", "Copy", priority=True, show=False),
        Binding("f8", "copy_transcript", "Copy transcript", priority=True, show=False),
        Binding("ctrl+p", "select_provider", "Provider", priority=True, show=False),
        Binding("ctrl+r", "select_session", "Sessions", priority=True, show=False),
        Binding("ctrl+e", "select_reasoning_effort", "Effort", priority=True, show=False),
        Binding("ctrl+comma", "open_settings", "Settings", priority=True, show=False),
        Binding("ctrl+q", "quit", "Quit", show=False),
        Binding("ctrl+l", "clear_transcript", "Clear", show=False),
        Binding("f1", "show_help", "Help", priority=True, show=False),
        Binding("escape", "collapse_active_tool_peek", "Summary", show=False),
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
        subagent_parent_capability_provider: Callable[[], SubagentCapabilitySet] | None = None,
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
        user_interaction: TuiUserInteraction | None = None,
        clipboard_writer: ClipboardWriter | None = None,
        socks_supported: bool = False,
    ) -> None:
        if context_window_tokens is not None and context_window_tokens <= 0:
            raise ValueError("context window tokens must be positive")
        super().__init__()
        self.register_theme(TEXTUAL_THEME)
        self.theme = TEXTUAL_THEME.name
        self._runner = runner
        self._user_interaction = user_interaction
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
        self._socks_supported = socks_supported
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
        self._subagent_parent_capability_provider = subagent_parent_capability_provider
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
        self._tool_activity_groups: list[ToolActivityGroupState] = []
        self._tool_activity_group_by_entry: dict[int, ToolActivityGroupState] = {}
        self._active_tool_activity_group: ToolActivityGroupState | None = None
        self._active_tool_inspector: _ActiveToolInspector | None = None
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
        self._turn_activity_started_at: float | None = None
        self._turn_activity_kind = "thinking"
        self._turn_activity_tool_name: str | None = None
        self._turn_activity_tool_started_at: float | None = None
        self._announced_terminal_tasks: set[str] = set()
        self._pending_auto_wake_tasks: set[str] = set()
        self._background_wake_state = BackgroundWakeState()
        self._background_wake_state_loaded = self._task_controller is None
        self._background_wake_active = False
        self._background_wake_task_ids: tuple[str, ...] = ()
        self._task_polling = False
        self._pending_interaction_request_id: str | None = None
        self._clipboard_writer = (
            SystemClipboardWriter() if clipboard_writer is None else clipboard_writer
        )
        self._last_clipboard_write = ClipboardWriteResult(native_copied=False)

    def copy_to_clipboard(self, text: str) -> None:
        """Copy through the native adapter while retaining Textual's OSC 52 fallback.

        通过原生适配器复制,并保留 Textual 的 OSC 52 终端回退路径。
        """

        self._last_clipboard_write = self._clipboard_writer.copy(text)
        super().copy_to_clipboard(text)

    def copy_text_to_clipboard(self, text: str) -> ClipboardWriteResult:
        """Copy text and return whether a native system clipboard accepted it.

        复制文本,并返回原生系统剪贴板是否已接受该文本。
        """

        self.copy_to_clipboard(text)
        return self._last_clipboard_write

    @property
    def entries(self) -> tuple[TranscriptEntry, ...]:
        return tuple(self._entries)

    def _main_screen_query_one(
        self,
        selector: str,
        expect_type: type[_WidgetType],
    ) -> _WidgetType:
        """Query persistent Conversation chrome even while a modal is active.

        ``App.query_one`` is scoped to the current screen. Tool lifecycle events
        continue while Inspector is pushed, so live Conversation updates must
        target the base screen instead of whichever modal currently has focus.
        """

        return self.screen_stack[0].query_one(selector, expect_type)

    def _main_screen_query_optional(
        self,
        selector: str,
        expect_type: type[_WidgetType],
    ) -> _WidgetType | None:
        """Return a base-screen widget, or ``None`` during screen teardown."""

        screen_stack = self.screen_stack
        if not screen_stack:
            return None
        candidate = next(iter(screen_stack[0].query(selector)), None)
        return candidate if isinstance(candidate, expect_type) else None

    def compose(self) -> ComposeResult:
        with Horizontal(id="header"):
            yield Static(id="brand")
            yield Static(id="header-space")
            yield Static(id="clock")
        yield VerticalScroll(id="transcript")
        with Vertical(id="composer"):
            yield Static(id="turn-activity")
            with Horizontal(id="prompt-row"):
                yield Static(_PROMPT_MARK, id="prompt-mark")
                yield PromptInput(
                    placeholder=ui_text(self._language, "prompt.placeholder"),
                    id="prompt",
                )
            yield Static(id="command-hints")
            yield Horizontal(
                Static(id="runtime-primary"),
                Static(id="runtime-secondary"),
                id="runtime-bar",
            )

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
            self.run_worker(
                self._announce_recovery_state(),
                name="crash-recovery-inspection",
                group="session",
                exclusive=False,
                exit_on_error=False,
            )
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
        prompt = self._main_screen_query_one("#prompt", PromptInput)
        prompt.sync_content_height()
        prompt.focus()

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
        self._main_screen_query_one("#brand", Static).update(brand)
        self._update_clock()

    def _update_clock(self) -> None:
        current_time = datetime.now(tz=UTC).astimezone()
        clock = self._main_screen_query_optional("#clock", Static)
        if clock is not None:
            clock.update(current_time.strftime("%H:%M"))

    async def _request_approval(self, request: PermissionRequest) -> PermissionApproval:
        return await self.push_screen_wait(
            PermissionApprovalScreen(request, language=self._language)
        )


__all__ = ["NeuroCodeApp"]
