from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, ClassVar, Protocol

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.geometry import Size
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import Button, Header, Input, Label, Static
from textual.worker import Worker

from neuro_code.domain.background_tasks import BackgroundTaskSnapshot, BackgroundTaskStatus
from neuro_code.domain.events import AgentEvent, AgentEventKind
from neuro_code.domain.messages import Message, Role, SessionItem
from neuro_code.domain.sessions import SessionSummary
from neuro_code.domain.ui_preferences import UiLanguage
from neuro_code.permissions import PermissionApproval, PermissionRequest
from neuro_code.ports.ui_preferences import UiPreferencesStore
from neuro_code.runtime.agent import AgentRunResult, EventSink
from neuro_code.runtime.approval import ApprovalHandler
from neuro_code.runtime.profile_conversation import (
    ProviderOption,
    ProviderSelectionResult,
    SessionOption,
    SessionSelectionResult,
)
from neuro_code.tui_text import language_name, ui_text

_RESTORED_MESSAGE_LIMIT = 20_000
_TASK_LIST_LIMIT = 20
_TASK_POLL_SECONDS = 0.5
_TERMINAL_SIZE_POLL_SECONDS = 0.25

_NEURO_CODE_THEME = Theme(
    name="neuro-code-dark",
    primary="#c7a15a",
    secondary="#777c83",
    accent="#c7a15a",
    warning="#d0a45a",
    error="#c76d6d",
    success="#8fa481",
    foreground="#d8dadd",
    background="#101214",
    surface="#1a1c1f",
    panel="#16181b",
    boost="#2a2d31",
    luminosity_spread=0.08,
    text_alpha=0.96,
    variables={
        "border": "#806b48",
        "border-blurred": "#3b3f44",
        "block-cursor-background": "#c7a15a",
        "block-cursor-foreground": "#101214",
        "block-hover-background": "#2a2d31",
        "button-color-foreground": "#101214",
        "button-focus-text-style": "bold",
        "footer-background": "#141619",
        "footer-description-background": "#141619",
        "footer-description-foreground": "#a9adb3",
        "footer-item-background": "#141619",
        "footer-key-background": "#141619",
        "footer-key-foreground": "#c7a15a",
        "input-cursor-background": "#d8dadd",
        "input-cursor-foreground": "#101214",
        "input-selection-background": "#806b48 55%",
        "scrollbar": "#50555b",
        "scrollbar-active": "#806b48",
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


class ConversationMessage(Static):
    """One stable message node in the scrollable conversation."""

    def __init__(self, category: str, rendered: Text, *, pending: bool = False) -> None:
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
        padding: 1 1;
        background: #101214;
        color: #d8dadd;
        border-bottom: solid #303338;
    }

    .conversation-message {
        width: 100%;
        height: auto;
        min-height: 1;
        margin-bottom: 1;
        padding: 0 1;
        color: #d8dadd;
    }

    .message-user {
        padding: 1 1;
        background: #292c30;
        color: #e3e5e8;
        border-left: solid #806b48;
    }

    .message-assistant {
        background: #101214;
        color: #e1e3e6;
    }

    .message-pending {
        color: #8e939a;
        text-style: italic;
    }

    .message-system {
        color: #c7a15a;
    }

    .message-tool {
        color: #b59663;
    }

    .message-status {
        color: #8e939a;
    }

    .message-error {
        color: #c76d6d;
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
        border: tall #806b48;
    }

    #shortcut-bar {
        height: 1;
        padding: 0 1;
        background: #141619;
        color: #a9adb3;
    }

    #provider-options Button:focus,
    #session-options Button:focus,
    #settings-languages Button:focus {
        background: #292c30;
        color: #e1e3e6;
        border-left: solid #806b48;
    }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+c", "cancel_turn", "Cancel", priority=True, show=False),
        Binding("ctrl+p", "select_provider", "Provider", priority=True, show=False),
        Binding("ctrl+r", "select_session", "Sessions", priority=True, show=False),
        Binding("ctrl+comma", "open_settings", "Settings", priority=True, show=False),
        Binding("ctrl+q", "quit", "Quit", show=False),
        Binding("ctrl+l", "clear_transcript", "Clear", show=False),
    ]

    def __init__(
        self,
        runner: ConversationRunner,
        *,
        approval_controller: ApprovalController | None = None,
        provider_controller: ProviderController | None = None,
        session_controller: SessionController | None = None,
        task_controller: TaskController | None = None,
        ui_preferences: UiPreferencesStore | None = None,
        language: UiLanguage = UiLanguage.ENGLISH,
        initial_items: Sequence[SessionItem] = (),
        provider_name: str,
        model_name: str,
        cwd: Path,
    ) -> None:
        super().__init__()
        self.register_theme(_NEURO_CODE_THEME)
        self.theme = _NEURO_CODE_THEME.name
        self._runner = runner
        self._approval_controller = approval_controller
        self._provider_controller = provider_controller
        self._session_controller = session_controller
        self._task_controller = task_controller
        self._ui_preferences = ui_preferences
        self._language = language
        self._initial_items = tuple(initial_items)
        self._provider_name = provider_name
        self._model_name = model_name
        self._cwd = cwd
        self._entries: list[TranscriptEntry] = []
        self._entry_widgets: list[ConversationMessage] = []
        self._assistant_parts: list[str] = []
        self._pending_assistant: ConversationMessage | None = None
        self._reasoning_announced = False
        self._turn_worker: Worker[None] | None = None
        self._announced_terminal_tasks: set[str] = set()
        self._task_polling = False

    @property
    def entries(self) -> tuple[TranscriptEntry, ...]:
        return tuple(self._entries)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True, icon="")
        yield VerticalScroll(id="transcript")
        yield Input(
            placeholder=ui_text(self._language, "prompt.placeholder"),
            id="prompt",
        )
        yield Static(ui_text(self._language, "shortcuts"), id="shortcut-bar")

    def on_mount(self) -> None:
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
        self._assistant_parts.clear()
        self._reasoning_announced = False
        self._begin_pending_assistant()
        self._turn_worker = self.run_worker(
            self._run_prompt(prompt),
            name="agent-turn",
            group="agent",
            exclusive=True,
            exit_on_error=False,
        )

    async def _run_prompt(self, prompt: str) -> None:
        prompt_input = self.query_one("#prompt", Input)
        try:
            result = await self._runner.run(prompt, sink=self._handle_event)
            response = result.response or ui_text(self._language, "turn.no_response")
            self._finish_pending_assistant(response)
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
            key = (
                "provider.fallback_selected"
                if data.get("failover") is True
                else "provider.selected"
            )
            self._write_ui_entry("status", key, provider=provider, model=model)
        elif event.kind is AgentEventKind.BACKEND_TOOL_STARTED:
            self._write_ui_entry(
                "tool",
                "tool.hosted_started",
                name=self._field(data, "name"),
            )
        elif event.kind is AgentEventKind.BACKEND_TOOL_COMPLETED:
            self._write_ui_entry(
                "tool",
                "tool.hosted_completed",
                name=self._field(data, "name"),
            )
        elif event.kind is AgentEventKind.TOOL_REQUESTED:
            self._write_ui_entry(
                "tool",
                "tool.requested",
                name=self._field(data, "name"),
            )
        elif event.kind is AgentEventKind.TOOL_PERMISSION:
            effect = self._field(data, "effect")
            if effect == "deny":
                self._write_ui_entry(
                    "error",
                    "tool.permission_denied",
                    name=self._field(data, "name"),
                    effect=effect,
                    reason=self._field(data, "reason"),
                )
        elif event.kind is AgentEventKind.TOOL_APPROVAL_REQUESTED:
            self._write_ui_entry(
                "status",
                "tool.awaiting_approval",
                name=self._field(data, "name"),
            )
        elif event.kind is AgentEventKind.TOOL_APPROVAL_RESOLVED:
            effect = self._field(data, "effect")
            outcome = self._field(data, "outcome")
            category = "status" if effect == "allow" else "error"
            self._write_ui_entry(
                category,
                "tool.approval_resolved",
                name=self._field(data, "name"),
                outcome=outcome,
            )
        elif event.kind is AgentEventKind.TOOL_COMPLETED:
            self._write_ui_entry(
                "tool",
                "tool.completed",
                name=self._field(data, "name"),
            )
        elif event.kind is AgentEventKind.TOOL_FAILED:
            self._write_ui_entry(
                "error",
                "tool.failed",
                name=self._field(data, "name"),
            )

    def _field(self, data: Mapping[str, Any], name: str) -> str:
        value = data.get(name)
        if isinstance(value, str) and value:
            return value
        return ui_text(self._language, "value.unknown")

    async def _dispatch_slash_command(self, raw: str) -> None:
        command, _, arguments = raw[1:].partition(" ")
        command = command.casefold()
        if command in {"model", "provider"}:
            await self._select_provider(arguments.strip() or None)
            return
        if command in {"resume", "sessions"}:
            requested = arguments.strip() or None
            if command == "sessions":
                await self._select_session(None, query=requested)
            else:
                await self._select_session(requested)
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
        self._write_ui_entry("system", "transcript.cleared")

    def action_cancel_turn(self) -> None:
        if isinstance(self.screen, PermissionApprovalScreen):
            self.screen.action_deny()
            return
        if isinstance(self.screen, ProviderSelectionScreen):
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

    async def action_select_session(self) -> None:
        await self._select_session(None)

    async def action_open_settings(self) -> None:
        self.push_screen(
            SettingsScreen(self._language, language=self._language),
            self._settings_selected,
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

    def _render_entry(self, category: str, content: str) -> Text:
        if category == "user":
            rendered = Text("> ", style="bold #a9adb3")
            rendered.append(content, style="#e3e5e8")
            return rendered
        if category == "assistant":
            rendered = Text("● ", style="bold #e1e3e6")
            rendered.append(content, style="#e1e3e6")
            return rendered

        labels = {
            "error": (ui_text(self._language, "label.error"), "bold #c76d6d"),
            "status": (ui_text(self._language, "label.status"), "#8e939a"),
            "system": ("Neuro Code", "bold #c7a15a"),
            "tool": (ui_text(self._language, "label.tool"), "bold #b59663"),
        }
        label, style = labels.get(category, (category.title(), ""))
        rendered = Text(f"{label}  ", style=style)
        rendered.append(content)
        return rendered

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
        widget = ConversationMessage(category, self._render_entry(category, content))
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
        self.query_one("#shortcut-bar", Static).update(ui_text(self._language, "shortcuts"))

    def _refresh_localized_interface(self) -> None:
        self._apply_language_to_chrome()
        for index, (entry, widget) in enumerate(
            zip(self._entries, self._entry_widgets, strict=True)
        ):
            if entry.ui_key is not None:
                content = ui_text(
                    self._language,
                    entry.ui_key,
                    **dict(entry.ui_values),
                )
                entry = replace(entry, text=content)
                self._entries[index] = entry
            widget.update(self._render_entry(entry.category, entry.text))
        if self._pending_assistant is not None:
            content = (
                "".join(self._assistant_parts)
                if self._assistant_parts
                else ui_text(self._language, "turn.waiting")
            )
            self._pending_assistant.update(self._render_entry("assistant", content))


__all__ = [
    "ApprovalController",
    "ConversationMessage",
    "ConversationRunner",
    "NeuroCodeApp",
    "PermissionApprovalScreen",
    "ProviderController",
    "ProviderSelectionScreen",
    "SessionController",
    "SessionSelectionScreen",
    "SettingsScreen",
    "TaskController",
    "TranscriptEntry",
]
