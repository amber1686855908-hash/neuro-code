from __future__ import annotations

import asyncio
import difflib
import os
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, ClassVar, Protocol

from rich.console import RenderableType
from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text
from rich.theme import Theme as RichTheme
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.geometry import Size
from textual.screen import ModalScreen
from textual.suggester import Suggester
from textual.theme import Theme
from textual.widgets import Button, Header, Input, Label, Static
from textual.worker import Worker

from neuro_code.domain.background_tasks import BackgroundTaskSnapshot, BackgroundTaskStatus
from neuro_code.domain.context_usage import estimate_context_tokens, estimate_text_tokens
from neuro_code.domain.events import AgentEvent, AgentEventKind
from neuro_code.domain.interaction_mode import InteractionMode
from neuro_code.domain.messages import Message, Role, SessionItem
from neuro_code.domain.reasoning import ReasoningEffort
from neuro_code.domain.sessions import SessionSummary
from neuro_code.domain.ui_preferences import UiLanguage
from neuro_code.permissions import PermissionApproval, PermissionRequest
from neuro_code.ports.ui_preferences import UiPreferencesStore
from neuro_code.redaction import redact_sensitive_text
from neuro_code.runtime.agent import AgentRunResult, EventSink
from neuro_code.runtime.approval import ApprovalHandler
from neuro_code.runtime.profile_conversation import (
    InteractionModeSelectionResult,
    ProviderOption,
    ProviderSelectionResult,
    ReasoningEffortSelectionResult,
    SessionOption,
    SessionSelectionResult,
)
from neuro_code.tui_commands import SlashCompletion, slash_completions
from neuro_code.tui_text import language_name, ui_text

_RESTORED_MESSAGE_LIMIT = 20_000
_TASK_LIST_LIMIT = 20
_TASK_POLL_SECONDS = 0.5
_TERMINAL_SIZE_POLL_SECONDS = 0.25
_COMMAND_HINT_LIMIT = 5
_TOOL_OUTPUT_MAX_LINES = 40
_TOOL_OUTPUT_MAX_CHARACTERS = 6_000
_TOOL_DIFF_MAX_FILES = 8
_ANSI_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")

_EFFORT_COLORS = {
    ReasoningEffort.LOW: "#8e939a",
    ReasoningEffort.MEDIUM: "#6fb7d6",
    ReasoningEffort.HIGH: "#78c2a4",
    ReasoningEffort.XHIGH: "#a9a1e8",
    ReasoningEffort.ULTRACODE: "#d58cc8",
}

_MODE_COLORS = {
    InteractionMode.NORMAL: "#9aa3b2",
    InteractionMode.ACCEPT_EDITS: "#78c2a4",
    InteractionMode.PLAN: "#8b9cff",
    InteractionMode.AUTO: "#d58cc8",
}

_MARKDOWN_THEME = RichTheme(
    {
        "markdown.paragraph": "#d8dadd",
        "markdown.text": "#d8dadd",
        "markdown.em": "italic #b8bcc2",
        "markdown.strong": "bold #aebcff",
        "markdown.code": "bold #9cc4cc on #1a1c1f",
        "markdown.code_block": "#d8dadd on #16181b",
        "markdown.block_quote": "italic #9aa2ad",
        "markdown.list": "#d8dadd",
        "markdown.item": "#d8dadd",
        "markdown.item.bullet": "bold #6fc3df",
        "markdown.item.number": "bold #6fc3df",
        "markdown.hr": "#3b3f44",
        "markdown.h1": "bold #b6c2ff",
        "markdown.h2": "bold #9eafff",
        "markdown.h3": "bold #8b9cff",
        "markdown.h4": "bold #8ed1e6",
        "markdown.h5": "bold #9db4c8",
        "markdown.h6": "bold #989da4",
        "markdown.link": "underline #7da7d9",
        "markdown.link_url": "underline #7da7d9",
        "markdown.table.border": "#50555b",
        "markdown.table.header": "bold #9eafff",
        "markdown.kbd": "bold #aebcff on #292c30",
    }
)

_NEURO_CODE_THEME = Theme(
    name="neuro-code-dark",
    primary="#8b9cff",
    secondary="#777c83",
    accent="#6fc3df",
    warning="#e59c74",
    error="#c76d6d",
    success="#78c2a4",
    foreground="#d8dadd",
    background="#101214",
    surface="#1a1c1f",
    panel="#16181b",
    boost="#2a2d31",
    luminosity_spread=0.08,
    text_alpha=0.96,
    variables={
        "border": "#5866a3",
        "border-blurred": "#3b3f44",
        "block-cursor-background": "#8b9cff",
        "block-cursor-foreground": "#101214",
        "block-hover-background": "#2a2d31",
        "button-color-foreground": "#101214",
        "button-focus-text-style": "bold",
        "footer-background": "#141619",
        "footer-description-background": "#141619",
        "footer-description-foreground": "#a9adb3",
        "footer-item-background": "#141619",
        "footer-key-background": "#141619",
        "footer-key-foreground": "#8b9cff",
        "input-cursor-background": "#d8dadd",
        "input-cursor-foreground": "#101214",
        "input-selection-background": "#5866a3 55%",
        "scrollbar": "#50555b",
        "scrollbar-active": "#6878ba",
        "scrollbar-background": "#101214",
        "scrollbar-hover": "#666b72",
    },
)


def _read_terminal_size() -> Size | None:
    """Read the real TTY viewport without trusting possibly stale shell variables."""

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

    async def run(self, prompt: str, *, sink: EventSink | None = None) -> AgentRunResult: ...


class ApprovalController(Protocol):
    def set_handler(self, handler: ApprovalHandler | None) -> None: ...


class ProviderController(Protocol):
    @property
    def profiles(self) -> tuple[ProviderOption, ...]: ...

    @property
    def selected_profile(self) -> str: ...

    async def select_profile(self, name: str) -> ProviderSelectionResult: ...


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


class SessionController(Protocol):
    async def list_sessions(self, query: str | None = None) -> tuple[SessionOption, ...]: ...

    async def select_session(self, session_id: str) -> SessionSelectionResult: ...

    async def rename_session(self, title: str) -> SessionSummary: ...


class TaskController(Protocol):
    async def list_background_tasks(self) -> tuple[BackgroundTaskSnapshot, ...]: ...


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
    content: str | None = None
    is_error: bool = False
    metadata: dict[str, Any] | None = None
    workspace_changes: dict[str, Any] | None = None


class AssistantMarkdown(Markdown):
    """Safe model Markdown whose string form remains useful in diagnostics."""

    def __str__(self) -> str:
        return self.markup


class SlashCommandSuggester(Suggester):
    """Show the same first completion that Tab will apply."""

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
    """One stable message node in the scrollable conversation."""

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


class SettingsScreen(ModalScreen[UiLanguage | None]):
    """Choose an application-owned interface language."""

    CSS = """
    SettingsScreen {
        align: center middle;
        background: $background 70%;
    }

    #settings-dialog {
        width: 70%;
        max-width: 72;
        height: auto;
        padding: 1 2;
        border: heavy $primary;
        background: $surface;
    }

    #settings-title {
        text-style: bold;
        color: $primary;
        margin-bottom: 1;
    }

    #settings-description,
    #settings-help {
        color: $text-muted;
        margin-bottom: 1;
    }

    #settings-languages {
        height: auto;
    }

    #settings-languages Button {
        width: 1fr;
        margin-right: 1;
    }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("ctrl+c", "cancel", "Cancel", show=False),
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
            Label(ui_text(self.language, "settings.title"), id="settings-title"),
            Static(
                ui_text(self.language, "settings.description"),
                id="settings-description",
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
            Static(ui_text(self.language, "settings.help"), id="settings-help"),
            id="settings-dialog",
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

    def action_cancel(self) -> None:
        self.dismiss(None)


class ReasoningEffortScreen(ModalScreen[ReasoningEffort | None]):
    """Select application-owned review depth without implying native API support."""

    CSS = """
    ReasoningEffortScreen {
        align: center middle;
        background: $background 70%;
    }

    #effort-dialog {
        width: 90%;
        max-width: 100;
        height: auto;
        max-height: 90%;
        padding: 1 2;
        border: heavy $primary;
        background: $surface;
    }

    #effort-title {
        text-style: bold;
        color: $primary;
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
        color = _EFFORT_COLORS[effort]
        rendered = Text(f"{effort.glyph}  {effort.value}", style=f"bold {color}")
        rendered.append(
            f"  ·  {ui_text(self.language, f'effort.description.{effort.value}')}",
            style="#b0b4ba",
        )
        if effort is self.selected:
            rendered.append(
                f"  ({ui_text(self.language, 'marker.current')})",
                style="bold #8b9cff",
            )
        if effort is ReasoningEffort.ULTRACODE:
            rendered.append(
                f"  ({ui_text(self.language, 'effort.workflow_planned')})",
                style="#d58cc8",
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
    """Fail-closed modal for one bounded permission request."""

    CSS = """
    PermissionApprovalScreen {
        align: center middle;
        background: $background 70%;
    }

    #approval-dialog {
        width: 80%;
        max-width: 100;
        height: auto;
        max-height: 90%;
        padding: 1 2;
        border: heavy $warning;
        background: $surface;
    }

    #approval-title {
        text-style: bold;
        color: $warning;
        margin-bottom: 1;
    }

    #approval-summary {
        height: auto;
        max-height: 12;
        overflow-y: auto;
        margin: 1 0;
        padding: 1;
        border: round $primary-darken-2;
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
    """Select one configured profile without exposing credentials or endpoints."""

    CSS = """
    ProviderSelectionScreen {
        align: center middle;
        background: $background 70%;
    }

    #provider-dialog {
        width: 90%;
        max-width: 110;
        height: 80%;
        padding: 1 2;
        border: heavy $primary;
        background: $surface;
    }

    #provider-title {
        text-style: bold;
        color: $primary;
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
    """Select one recent session already constrained to the active workspace."""

    CSS = """
    SessionSelectionScreen {
        align: center middle;
        background: $background 70%;
    }

    #session-dialog {
        width: 90%;
        max-width: 115;
        height: 80%;
        padding: 1 2;
        border: heavy $primary;
        background: $surface;
    }

    #session-title {
        text-style: bold;
        color: $primary;
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
    ) -> None:
        super().__init__()
        self.options = options
        self.search_query = query
        self.language = language
        self._choice_ids = {
            f"session-choice-{index}": option.session_id for index, option in enumerate(options)
        }

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
        buttons = [
            Button(
                Text(self._label(option, self.language)),
                id=f"session-choice-{index}",
                variant="primary" if option.current else "default",
                disabled=not option.selectable,
                tooltip=option.session_id,
            )
            for index, option in enumerate(self.options)
        ]
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
            VerticalScroll(*buttons, id="session-options"),
            Static(
                ui_text(self.language, "session.help"),
                id="session-help",
            ),
            id="session-dialog",
        )

    def on_mount(self) -> None:
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

    def on_button_pressed(self, event: Button.Pressed) -> None:
        session_id = self._choice_ids.get(event.button.id or "")
        if session_id is not None:
            self.dismiss(session_id)

    def action_cancel(self) -> None:
        self.dismiss(None)


class NeuroCodeApp(App[None]):
    """Minimal Textual interface over the normalized agent event stream."""

    TITLE = "Neuro Code"
    SUB_TITLE = "Terminal coding agent"
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    Screen {
        layout: vertical;
        width: 100%;
        height: 100%;
        background: #101214;
        color: #d8dadd;
    }

    Header {
        background: #16181b;
        color: #d8dadd;
    }

    HeaderIcon {
        display: none;
    }

    HeaderTitle {
        padding-left: 2;
        content-align: left middle;
    }

    HeaderClock {
        background: #1a1c1f;
        color: #989da4;
    }

    #transcript {
        width: 100%;
        height: 1fr;
        padding: 1 2;
        background: #101214;
        color: #d8dadd;
        border-bottom: solid #303338;
    }

    .conversation-message {
        width: 100%;
        height: auto;
        min-height: 1;
        margin-bottom: 1;
        padding: 0;
        color: #d8dadd;
    }

    .message-user {
        padding: 1 2;
        background: #292c30;
        color: #e3e5e8;
        border-left: solid #5866a3;
    }

    .message-assistant {
        padding: 0 2;
        background: #101214;
        color: #e1e3e6;
    }

    .message-pending {
        color: #8e939a;
        text-style: italic;
    }

    .message-system {
        padding: 0 1;
        color: #aebcff;
    }

    .message-tool {
        padding: 0 1;
        color: #78c2a4;
    }

    .message-status {
        padding: 0 1;
        color: #8e939a;
    }

    .message-error {
        padding: 0 1;
        color: #c76d6d;
    }

    #runtime-bar {
        width: 100%;
        height: 1;
        padding: 0 2;
        background: #16181b;
        color: #a9adb3;
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

    #prompt {
        height: 3;
        margin: 0 1;
        background: #1a1c1f;
        color: #d8dadd;
        border: tall #3b3f44;
    }

    #prompt:focus {
        background: #1d1f22;
        background-tint: #ffffff 2%;
        border: tall #5866a3;
    }

    #command-hints {
        display: none;
        width: 100%;
        height: auto;
        max-height: 3;
        padding: 0 2;
        background: #141619;
        color: #a9adb3;
        overflow: hidden hidden;
    }

    #shortcut-bar {
        height: 1;
        padding: 0 1;
        background: #141619;
        color: #a9adb3;
    }

    #provider-options Button:focus,
    #session-options Button:focus,
    #effort-options Button:focus,
    #settings-languages Button:focus {
        background: #292c30;
        color: #e1e3e6;
        border-left: solid #5866a3;
    }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+c", "cancel_turn", "Cancel", priority=True, show=False),
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
        approval_controller: ApprovalController | None = None,
        provider_controller: ProviderController | None = None,
        reasoning_controller: ReasoningController | None = None,
        interaction_mode_controller: InteractionModeController | None = None,
        session_controller: SessionController | None = None,
        task_controller: TaskController | None = None,
        ui_preferences: UiPreferencesStore | None = None,
        language: UiLanguage = UiLanguage.ENGLISH,
        initial_items: Sequence[SessionItem] = (),
        provider_name: str,
        model_name: str,
        cwd: Path,
        reasoning_effort: ReasoningEffort = ReasoningEffort.HIGH,
        interaction_mode: InteractionMode = InteractionMode.NORMAL,
        context_window_tokens: int | None = None,
    ) -> None:
        if context_window_tokens is not None and context_window_tokens <= 0:
            raise ValueError("context window tokens must be positive")
        super().__init__()
        self.register_theme(_NEURO_CODE_THEME)
        self.theme = _NEURO_CODE_THEME.name
        self._runner = runner
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
        self._task_controller = task_controller
        self._ui_preferences = ui_preferences
        self._language = language
        self._initial_items = tuple(initial_items)
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
        self._entries: list[TranscriptEntry] = []
        self._entry_widgets: list[ConversationMessage] = []
        self._tool_feedback_by_call: dict[tuple[bool, str], ToolFeedbackState] = {}
        self._tool_feedback_by_entry: dict[int, ToolFeedbackState] = {}
        self._assistant_parts: list[str] = []
        self._pending_assistant: ConversationMessage | None = None
        self._reasoning_announced = False
        self._turn_completion: tuple[str, int] | None = None
        self._turn_usage_reported = False
        self._turn_worker: Worker[None] | None = None
        self._announced_terminal_tasks: set[str] = set()
        self._task_polling = False

    @property
    def entries(self) -> tuple[TranscriptEntry, ...]:
        return tuple(self._entries)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True, icon="")
        yield VerticalScroll(id="transcript")
        yield Horizontal(
            Static(id="runtime-model"),
            Static(id="runtime-workspace"),
            Static(id="runtime-context"),
            Static(id="runtime-effort"),
            Static(id="runtime-mode"),
            id="runtime-bar",
        )
        yield Input(
            placeholder=ui_text(self._language, "prompt.placeholder"),
            suggester=SlashCommandSuggester(self._slash_completions),
            id="prompt",
        )
        yield Static(id="command-hints")
        yield Static(ui_text(self._language, "shortcuts"), id="shortcut-bar")

    def on_mount(self) -> None:
        self.console.push_theme(_MARKDOWN_THEME)
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
        if not self.is_headless and not self.is_inline and not self.is_web:
            self.set_interval(_TERMINAL_SIZE_POLL_SECONDS, self._synchronize_terminal_size)
        self.query_one("#prompt", Input).focus()

    def _synchronize_terminal_size(self) -> None:
        """Recover when a terminal drops its normal resize notification."""

        terminal_size = _read_terminal_size()
        if terminal_size is None or terminal_size == self.screen.size:
            return
        self.post_message(events.Resize(terminal_size, terminal_size, terminal_size))

    def on_unmount(self) -> None:
        if self._approval_controller is not None:
            self._approval_controller.set_handler(None)
        self.console.pop_theme()

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
            self._write_ui_entry("error", "turn.running")
            return

        self._write_entry("user", prompt)
        self._context_used_tokens += 4 + estimate_text_tokens(prompt)
        self._context_usage_estimated = True
        self._refresh_runtime_bar()
        self._assistant_parts.clear()
        self._reasoning_announced = False
        self._turn_completion = None
        self._turn_usage_reported = False
        self._begin_pending_assistant()
        self._turn_worker = self.run_worker(
            self._run_prompt(prompt),
            name="agent-turn",
            group="agent",
            exclusive=True,
            exit_on_error=False,
        )

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "prompt" and event.input.screen is self.screen:
            self._refresh_command_hints(event.value)

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
        prompt_input = self.query_one("#prompt", Input)
        try:
            result = await self._runner.run(prompt, sink=self._handle_event)
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
            if self._turn_completion is not None:
                duration, steps = self._turn_completion
                self._write_ui_entry(
                    "status",
                    "turn.completed",
                    duration=duration,
                    steps=steps,
                )
        except asyncio.CancelledError:
            await self._discard_pending_assistant()
            self._write_ui_entry("status", "turn.cancelled")
            raise
        except Exception as error:
            await self._discard_pending_assistant()
            self._write_entry("error", f"{type(error).__name__}: {error}")
        finally:
            prompt_input.focus()

    async def _handle_event(self, event: AgentEvent) -> None:
        data = event.data
        if event.kind is AgentEventKind.TEXT_DELTA:
            text = data.get("text")
            if isinstance(text, str):
                self._assistant_parts.append(text)
                self._update_pending_assistant("".join(self._assistant_parts))
        elif event.kind is AgentEventKind.REASONING_DELTA and not self._reasoning_announced:
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
        elif event.kind is AgentEventKind.TURN_COMPLETED:
            self._turn_completion = (
                self._event_duration(data),
                self._positive_int(data.get("step"), fallback=1),
            )

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
        elif event.kind is AgentEventKind.BACKEND_TOOL_COMPLETED:
            state.phase = "completed"
            state.duration = self._event_duration(data)
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
        elif event.kind in {AgentEventKind.TOOL_COMPLETED, AgentEventKind.TOOL_FAILED}:
            state.phase = "failed" if event.kind is AgentEventKind.TOOL_FAILED else "completed"
            state.duration = self._event_duration(data)
            state.content = self._optional_text(data.get("content"), allow_empty=True)
            state.is_error = (
                event.kind is AgentEventKind.TOOL_FAILED or data.get("is_error") is True
            )
            raw_metadata = data.get("metadata")
            state.metadata = dict(raw_metadata) if isinstance(raw_metadata, Mapping) else None
            raw_changes = data.get("workspace_changes")
            state.workspace_changes = (
                dict(raw_changes) if isinstance(raw_changes, Mapping) else None
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
        self._write_entry("tool", content)
        self._entry_widgets[state.entry_index].update(self._render_tool_feedback(state))
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

    def _refresh_tool_feedback(self, state: ToolFeedbackState) -> None:
        if state.entry_index >= len(self._entries):
            return
        transcript = self.query_one("#transcript", VerticalScroll)
        follow = transcript.is_vertical_scroll_end
        body = self._tool_feedback_body(state)
        self._entries[state.entry_index] = TranscriptEntry("tool", body.plain)
        self._entry_widgets[state.entry_index].update(self._render_tool_feedback(state, body=body))
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
        body = Text(overflow="fold")
        body.append("● ", style="bold #78c2a4")
        if state.hosted:
            body.append(ui_text(self._language, "tool.card.hosted"), style="#9aa3b2")
            body.append(" ")
        invocation = self._tool_invocation(state.name, state.arguments)
        body.append(invocation, style="bold #8ed1e6")

        if state.permission_effect == "allow":
            self._append_tool_line(
                body,
                "├",
                ui_text(
                    self._language,
                    "tool.card.allowed",
                    reason=self._bounded_inline(state.permission_reason),
                ),
                style="#9fb8af",
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
                    style="#9fb8af" if state.approval_effect == "allow" else "#d58b8b",
                )
            elif state.phase == "awaiting_approval":
                self._append_tool_line(
                    body,
                    "├",
                    ui_text(self._language, "tool.card.awaiting_approval"),
                    style="#a9adb3",
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
                    style="#a9adb3",
                )

        change_report = self._tool_change_report(state)
        if state.content is not None and (state.content or change_report is None):
            self._append_tool_output(body, state)
        if change_report is not None:
            self._append_tool_changes(body, change_report)

        if state.phase == "running":
            self._append_tool_line(
                body,
                "├",
                ui_text(self._language, "tool.card.running"),
                style="#a9adb3",
            )
        if state.phase == "completed":
            self._append_tool_line(
                body,
                "└",
                ui_text(
                    self._language,
                    "tool.card.completed",
                    duration=state.duration or "—",
                ),
                style="#78c2a4",
            )
        elif state.phase == "failed":
            self._append_tool_line(
                body,
                "└",
                ui_text(
                    self._language,
                    "tool.card.failed",
                    duration=state.duration or "—",
                ),
                style="#d58b8b",
            )
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
                style="#d58b8b",
            )
        return body

    @staticmethod
    def _append_tool_line(body: Text, connector: str, content: str, *, style: str) -> None:
        body.append("\n")
        body.append(f"{connector} ", style="#626a73")
        body.append(content, style=style)

    @staticmethod
    def _known_approval_outcome(outcome: str) -> str:
        return outcome if outcome in {"allow_once", "allow_session", "deny"} else "unknown"

    def _append_tool_output(self, body: Text, state: ToolFeedbackState) -> None:
        content = state.content or ""
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
            style="#d58b8b" if state.is_error else "#9aa3b2",
        )
        for line in lines:
            body.append("\n│   ", style="#505860")
            body.append(line, style="#c5c9ce")
        if omitted_lines:
            body.append("\n│   ", style="#505860")
            body.append(
                ui_text(self._language, "tool.card.lines_omitted", count=omitted_lines),
                style="italic #7f8790",
            )
        if truncated:
            body.append("\n│   ", style="#505860")
            body.append(
                ui_text(self._language, "tool.card.preview_truncated"),
                style="italic #7f8790",
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

    def _append_tool_changes(self, body: Text, report: Mapping[str, Any]) -> None:
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
            self._append_tool_line(body, "├", summary, style="#8ed1e6")
            body.highlight_words((path,), style="bold #8ed1e6", case_sensitive=True)

            raw_diff = change.get("diff")
            if isinstance(raw_diff, str) and raw_diff:
                diff_lines, _, omitted, truncated = self._bounded_tool_preview(raw_diff)
                for line in diff_lines:
                    body.append("\n│   ", style="#505860")
                    body.append(line, style=self._diff_line_style(line))
                if omitted or truncated or change.get("diff_truncated") is True:
                    body.append("\n│   ", style="#505860")
                    body.append(
                        ui_text(self._language, "tool.card.diff_truncated"),
                        style="italic #7f8790",
                    )
            hidden_reason = change.get("hidden_reason")
            if isinstance(hidden_reason, str):
                known_reason = (
                    hidden_reason
                    if hidden_reason in {"sensitive", "large", "budget", "binary", "redacted"}
                    else "unavailable"
                )
                body.append("\n│   ", style="#505860")
                body.append(
                    ui_text(self._language, f"tool.card.hidden.{known_reason}"),
                    style="italic #7f8790",
                )

        omitted_files = self._non_negative_int(report.get("omitted_files")) + max(
            0, len(files) - len(visible)
        )
        if omitted_files:
            self._append_tool_line(
                body,
                "├",
                ui_text(self._language, "tool.card.files_omitted", count=omitted_files),
                style="italic #7f8790",
            )
        if report.get("scan_limited") is True:
            self._append_tool_line(
                body,
                "├",
                ui_text(self._language, "tool.card.scan_limited"),
                style="italic #7f8790",
            )

    @staticmethod
    def _non_negative_int(value: object) -> int:
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0

    @staticmethod
    def _diff_line_style(line: str) -> str:
        if line.startswith("@@"):
            return "#8b9cff"
        if line.startswith(("+++", "---")):
            return "bold #7da7d9"
        if line.startswith("+"):
            return "#78c2a4"
        if line.startswith("-"):
            return "#d07878"
        return "#b8bcc2"

    async def _dispatch_slash_command(self, raw: str) -> None:
        command, _, arguments = raw[1:].partition(" ")
        command = command.casefold()
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
            await self._show_background_tasks()
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

    def action_clear_transcript(self) -> None:
        transcript = self.query_one("#transcript", VerticalScroll)
        transcript.remove_children(tuple(self._entry_widgets))
        self._entries.clear()
        self._entry_widgets.clear()
        self._tool_feedback_by_call.clear()
        self._tool_feedback_by_entry.clear()
        self._write_ui_entry("system", "transcript.cleared")

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
        if isinstance(self.screen, SettingsScreen):
            self.screen.action_cancel()
            return
        if self._turn_worker is not None and self._turn_worker.is_running:
            self._write_ui_entry("status", "turn.cancel_requested")
            self._turn_worker.cancel()
            return
        prompt = self.query_one("#prompt", Input)
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
            SettingsScreen(self._language, language=self._language),
            self._settings_selected,
        )

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

    async def _settings_selected(self, language: UiLanguage | None) -> None:
        if language is None or language is self._language:
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
            result = await self._provider_controller.select_profile(profile_name)
        except Exception as error:
            self._write_entry("error", f"{type(error).__name__}: {error}")
            return

        self._provider_name = result.provider_name
        self._model_name = result.model_name
        self._context_window_tokens = result.context_window_tokens
        if result.changed:
            self._context_used_tokens = 0
            self._context_usage_estimated = True
        self._refresh_runtime_bar()
        if result.changed:
            self._reset_background_task_tracking()
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
        if self._session_controller is None:
            self._write_ui_entry("error", "session.resume_unavailable")
            return
        if self._turn_worker is not None and self._turn_worker.is_running:
            self._write_ui_entry("error", "session.resume_running")
            return
        if requested is not None:
            await self._apply_session_selection(requested)
            return
        try:
            options = await self._session_controller.list_sessions(query)
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
            SessionSelectionScreen(options, query=query, language=self._language),
            self._session_selected,
        )

    async def _rename_session(self, title: str) -> None:
        if self._session_controller is None:
            self._write_ui_entry("error", "session.rename_unavailable")
            return
        if self._turn_worker is not None and self._turn_worker.is_running:
            self._write_ui_entry("error", "session.rename_running")
            return
        try:
            summary = await self._session_controller.rename_session(title)
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
        assert self._session_controller is not None
        try:
            result = await self._session_controller.select_session(session_id)
        except Exception as error:
            self._write_entry("error", f"{type(error).__name__}: {error}")
            return

        self._provider_name = result.provider_name
        self._model_name = result.model_name
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

        self._reset_background_task_tracking()
        self._replace_transcript(result.items)
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

    async def _show_background_tasks(self) -> None:
        if self._task_controller is None:
            self._write_ui_entry("error", "tasks.unavailable")
            return
        try:
            snapshots = await self._task_controller.list_background_tasks()
        except Exception as error:
            self._write_entry("error", f"{type(error).__name__}: {error}")
            return
        if not snapshots:
            self._write_ui_entry("status", "tasks.none")
            return

        visible = snapshots[-_TASK_LIST_LIMIT:]
        omitted = len(snapshots) - len(visible)
        lines = [self._task_summary(snapshot) for snapshot in visible]
        if omitted:
            lines.insert(0, ui_text(self._language, "tasks.omitted", count=omitted))
        self._write_ui_entry(
            "system",
            "tasks.heading",
            lines="\n".join(lines),
        )

    async def _poll_background_tasks(self) -> None:
        if self._task_controller is None or self._task_polling:
            return
        self._task_polling = True
        try:
            snapshots = await self._task_controller.list_background_tasks()
        except Exception:
            return
        finally:
            self._task_polling = False

        for snapshot in snapshots:
            if not snapshot.status.terminal:
                continue
            if snapshot.task_id in self._announced_terminal_tasks:
                continue
            self._announced_terminal_tasks.add(snapshot.task_id)
            category = (
                "status"
                if snapshot.status
                in {BackgroundTaskStatus.COMPLETED, BackgroundTaskStatus.CANCELLED}
                else "error"
            )
            self._write_entry(category, self._task_completion_message(snapshot))

    def _reset_background_task_tracking(self) -> None:
        self._announced_terminal_tasks.clear()

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

    def _stopped_task_note(self, count: int) -> str:
        if count == 0:
            return ""
        if count == 1:
            return ui_text(self._language, "tasks.stopped_one")
        return ui_text(self._language, "tasks.stopped_many", count=count)

    def _replace_transcript(self, items: Sequence[SessionItem]) -> None:
        transcript = self.query_one("#transcript", VerticalScroll)
        transcript.remove_children()
        self._entries.clear()
        self._entry_widgets.clear()
        self._tool_feedback_by_call.clear()
        self._tool_feedback_by_entry.clear()
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

    def _bounded_restored_text(self, content: str) -> str:
        if len(content) <= _RESTORED_MESSAGE_LIMIT:
            return content
        return (
            f"{content[:_RESTORED_MESSAGE_LIMIT]}\n{ui_text(self._language, 'restore.truncated')}"
        )

    @staticmethod
    def _semantic_value_style(name: str, value: object) -> str | None:
        if name in {"provider", "model", "profile", "source"}:
            return "bold #7da7d9"
        if name in {"name", "task_id", "session_id", "title"}:
            return "bold #8ed1e6"
        if name in {"cwd", "path"}:
            return "#8ab3ad"
        if name in {"effect", "outcome", "status"}:
            return "bold #78c2a4"
        if name in {"duration", "steps", "step"}:
            return "bold #78c2a4"
        if name == "context":
            return "bold #8ab3ad"
        if name in {"effort", "requested", "effective"}:
            try:
                effort = ReasoningEffort(str(value))
            except ValueError:
                return "bold #a9a1e8"
            return f"bold {_EFFORT_COLORS[effort]}"
        if name == "mode":
            try:
                mode = InteractionMode(str(value))
            except ValueError:
                return "bold #8b9cff"
            return f"bold {_MODE_COLORS[mode]}"
        if name == "policy":
            return "#9aa3b2"
        if name in {"message", "reason", "error"}:
            return "#d88a8a"
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
            return Text(content, style="#e3e5e8", overflow="fold")
        if category == "assistant":
            return AssistantMarkdown(
                content,
                code_theme="github-dark",
                style="#e1e3e6",
                hyperlinks=False,
            )

        labels = {
            "error": (ui_text(self._language, "label.error"), "bold #c76d6d"),
            "status": (ui_text(self._language, "label.status"), "bold #8e939a"),
            "system": ("Neuro Code", "bold #8b9cff"),
            "tool": (ui_text(self._language, "label.tool"), "bold #78c2a4"),
        }
        body_styles = {
            "error": "#d58b8b",
            "status": "#a9adb3",
            "system": "#c8cbd0",
            "tool": "#b8c7c1",
        }
        label, label_style = labels.get(category, (category.title(), "bold"))
        body = Text(content, style=body_styles.get(category, "#d8dadd"), overflow="fold")
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
        label_style = "bold #d07878" if state.is_error else "bold #78c2a4"
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
    ) -> None:
        entry = TranscriptEntry(category, content, ui_key, ui_values)
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
        waiting = ui_text(self._language, "turn.waiting")
        pending = ConversationMessage(
            "assistant",
            self._render_entry("assistant", waiting),
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
        if follow:
            transcript.scroll_end(animate=False)

    async def _discard_pending_assistant(self) -> None:
        pending = self._pending_assistant
        self._pending_assistant = None
        self._assistant_parts.clear()
        if pending is not None and pending.parent is not None:
            await pending.remove()

    def _apply_language_to_chrome(self) -> None:
        self.sub_title = ui_text(self._language, "subtitle")
        self.query_one("#prompt", Input).placeholder = ui_text(
            self._language,
            "prompt.placeholder",
        )
        shortcuts = Text(ui_text(self._language, "shortcuts"), style="#a9adb3")
        shortcuts.highlight_regex(r"\^(?:[A-Z]|,)|Shift\+Tab", style="bold #8b9cff")
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
        hints.append(ui_text(self._language, "command_hint.tab"), style="bold #8b9cff")
        hints.append("  ", style="#555b62")
        for index, completion in enumerate(completions[:_COMMAND_HINT_LIMIT]):
            if index:
                hints.append("  ·  ", style="#555b62")
            hints.append(completion.display, style="#7da7d9")
        if len(completions) > _COMMAND_HINT_LIMIT:
            hints.append("  ·  …", style="#777c83")
        widget.update(hints)
        widget.display = True

    def _context_percentage(self) -> str:
        window = self._context_window_tokens
        if window is None:
            return ui_text(self._language, "runtime.context_unknown")
        percentage = self._context_used_tokens / window * 100
        rendered = "<0.1%" if 0 < percentage < 0.1 else f"{percentage:.1f}%"
        return f"~{rendered}" if self._context_usage_estimated else rendered

    def _context_color(self) -> str:
        window = self._context_window_tokens
        if window is None:
            return "#8e939a"
        ratio = self._context_used_tokens / window
        if ratio >= 0.8:
            return "#c76d6d"
        if ratio >= 0.5:
            return "#e59c74"
        return "#78c2a4"

    def _context_usage_summary(self) -> str:
        window = self._context_window_tokens
        if window is None:
            return ui_text(self._language, "runtime.context_unknown")
        approximation = "≈" if self._context_usage_estimated else ""
        return (
            f"{self._context_percentage()} "
            f"({approximation}{self._context_used_tokens:,}/{window:,})"
        )

    def _refresh_runtime_bar(self) -> None:
        model = Text()
        model.append(
            f"{ui_text(self._language, 'runtime.model')}  ",
            style="bold #777c83",
        )
        model.append(self._provider_name, style="bold #8ab3ad")
        model.append(" / ", style="#555b62")
        model.append(self._model_name, style="bold #7da7d9")
        self.query_one("#runtime-model", Static).update(model)

        workspace = Text(justify="right", overflow="ellipsis", no_wrap=True)
        workspace.append(
            f"{ui_text(self._language, 'runtime.workspace')}  ",
            style="bold #777c83",
        )
        workspace.append(self._display_cwd(), style="#6fb7d6")
        workspace_widget = self.query_one("#runtime-workspace", Static)
        workspace_widget.update(workspace)
        workspace_widget.tooltip = str(self._cwd)

        context = Text()
        context.append(
            f"{ui_text(self._language, 'runtime.context')}  ",
            style="bold #777c83",
        )
        context.append(
            self._context_percentage(),
            style=f"bold {self._context_color()}",
        )
        context_widget = self.query_one("#runtime-context", Static)
        context_widget.update(context)
        if self._context_window_tokens is None:
            context_widget.tooltip = ui_text(
                self._language,
                "runtime.context_help_unknown",
            )
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
            style="bold #777c83",
        )
        effort.append(
            f"{requested.glyph} {requested.value}",
            style=f"bold {_EFFORT_COLORS[requested]}",
        )
        if effective is not requested:
            effort.append(" → ", style="#777c83")
            effort.append(
                f"{effective.glyph} {effective.value}",
                style=f"bold {_EFFORT_COLORS[effective]}",
            )
        effort_widget = self.query_one("#runtime-effort", Static)
        effort_widget.update(effort)
        effort_widget.tooltip = ui_text(self._language, "runtime.effort_help")

        mode = Text()
        mode.append(
            f"{ui_text(self._language, 'runtime.mode')}  ",
            style="bold #777c83",
        )
        mode.append(
            f"{self._interaction_mode.glyph} {self._interaction_mode.value}",
            style=f"bold {_MODE_COLORS[self._interaction_mode]}",
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
        except (OSError, ValueError):
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
            tool_state = self._tool_feedback_by_entry.get(index)
            if tool_state is not None:
                body = self._tool_feedback_body(tool_state)
                self._entries[index] = TranscriptEntry("tool", body.plain)
                widget.update(self._render_tool_feedback(tool_state, body=body))
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
            content = (
                "".join(self._assistant_parts)
                if self._assistant_parts
                else ui_text(self._language, "turn.waiting")
            )
            self._pending_assistant.update(self._render_entry("assistant", content))


__all__ = [
    "ApprovalController",
    "AssistantMarkdown",
    "ConversationMessage",
    "ConversationRunner",
    "NeuroCodeApp",
    "PermissionApprovalScreen",
    "ProviderController",
    "ProviderSelectionScreen",
    "ReasoningController",
    "ReasoningEffortScreen",
    "SessionController",
    "SessionSelectionScreen",
    "SettingsScreen",
    "TaskController",
    "TranscriptEntry",
]
