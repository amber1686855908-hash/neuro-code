from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Protocol

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Header, Input, Label, RichLog, Static
from textual.worker import Worker

from neuro_code.domain.background_tasks import BackgroundTaskSnapshot, BackgroundTaskStatus
from neuro_code.domain.events import AgentEvent, AgentEventKind
from neuro_code.domain.messages import Message, Role, SessionItem
from neuro_code.domain.sessions import SessionSummary
from neuro_code.permissions import PermissionApproval, PermissionRequest
from neuro_code.runtime.agent import AgentRunResult, EventSink
from neuro_code.runtime.approval import ApprovalHandler
from neuro_code.runtime.profile_conversation import (
    ProviderOption,
    ProviderSelectionResult,
    SessionOption,
    SessionSelectionResult,
)

_RESTORED_MESSAGE_LIMIT = 20_000
_TASK_LIST_LIMIT = 20
_TASK_POLL_SECONDS = 0.5


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

    def __init__(self, request: PermissionRequest) -> None:
        super().__init__()
        self.request = request

    def compose(self) -> ComposeResult:
        yield Vertical(
            Label("Tool approval required", id="approval-title"),
            Static(Text(f"Tool: {self.request.tool_name}")),
            Static(Text(self.request.summary), id="approval-summary"),
            Static(Text(f"Policy: {self.request.reason}"), id="approval-reason"),
            Horizontal(
                Button("Allow once [A]", variant="success", id="approval-allow-once"),
                Button(
                    "Allow identical action this session [S]",
                    variant="primary",
                    id="approval-allow-session",
                    disabled=self.request.scope_key is None,
                    tooltip=(
                        "Session approval is unavailable for an action that cannot be scoped safely."
                        if self.request.scope_key is None
                        else None
                    ),
                ),
                Button("Deny [D/Esc/Ctrl+C]", variant="error", id="approval-deny"),
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

    def __init__(self, options: tuple[ProviderOption, ...]) -> None:
        super().__init__()
        self.options = options
        self._choice_ids = {
            f"provider-choice-{index}": option.name for index, option in enumerate(options)
        }

    @staticmethod
    def _label(option: ProviderOption) -> str:
        markers: list[str] = []
        if option.selected:
            markers.append("current")
        if option.default:
            markers.append("default")
        if not option.available:
            markers.append("unavailable")
        elif not option.credential_configured:
            markers.append("credential missing")
        suffix = f" ({' · '.join(markers)})" if markers else ""
        return f"{option.name} · {option.model} · {option.protocol}{suffix}"

    def compose(self) -> ComposeResult:
        buttons = [
            Button(
                Text(self._label(option)),
                id=f"provider-choice-{index}",
                variant="primary" if option.selected else "default",
                disabled=not option.selectable,
            )
            for index, option in enumerate(self.options)
        ]
        yield Vertical(
            Label("Select provider profile", id="provider-title"),
            VerticalScroll(*buttons, id="provider-options"),
            Static(
                "Switching profile starts a new conversation. Esc/Ctrl+C closes this picker.",
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

    def __init__(self, options: tuple[SessionOption, ...], *, query: str | None = None) -> None:
        super().__init__()
        self.options = options
        self.search_query = query
        self._choice_ids = {
            f"session-choice-{index}": option.session_id for index, option in enumerate(options)
        }

    @staticmethod
    def _label(option: SessionOption) -> str:
        markers: list[str] = []
        if option.current:
            markers.append("current")
        if not option.source_profile_match:
            markers.append(f"resume via {option.resume_profile}")
        if option.sandbox_profile is None:
            markers.append("legacy sandbox")
        else:
            markers.append(f"sandbox {option.sandbox_profile.value}")
        if not option.sandbox_profile_match:
            markers.append("restart required")
        if not option.selectable:
            markers.append("unavailable")
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
                Text(self._label(option)),
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
                    f"Search workspace sessions: {self.search_query}"
                    if self.search_query is not None
                    else "Resume workspace session"
                ),
                id="session-title",
            ),
            VerticalScroll(*buttons, id="session-options"),
            Static(
                "Only sessions from this workspace are listed. "
                "Use /sessions QUERY for content search. Esc/Ctrl+C closes this picker.",
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
    CSS = """
    Screen {
        layout: vertical;
    }

    #transcript {
        height: 1fr;
        padding: 1 2;
        border-bottom: solid $primary-darken-2;
    }

    #stream {
        height: auto;
        min-height: 1;
        max-height: 8;
        padding: 0 2;
        color: $text;
    }

    #prompt {
        dock: bottom;
        margin: 0 1 1 1;
    }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+c", "cancel_turn", "Cancel", priority=True),
        Binding("ctrl+p", "select_provider", "Provider", priority=True),
        Binding("ctrl+r", "select_session", "Sessions", priority=True),
        Binding("ctrl+q", "quit", "Quit"),
        Binding("ctrl+l", "clear_transcript", "Clear"),
    ]

    def __init__(
        self,
        runner: ConversationRunner,
        *,
        approval_controller: ApprovalController | None = None,
        provider_controller: ProviderController | None = None,
        session_controller: SessionController | None = None,
        task_controller: TaskController | None = None,
        initial_items: Sequence[SessionItem] = (),
        provider_name: str,
        model_name: str,
        cwd: Path,
    ) -> None:
        super().__init__()
        self._runner = runner
        self._approval_controller = approval_controller
        self._provider_controller = provider_controller
        self._session_controller = session_controller
        self._task_controller = task_controller
        self._initial_items = tuple(initial_items)
        self._provider_name = provider_name
        self._model_name = model_name
        self._cwd = cwd
        self._entries: list[TranscriptEntry] = []
        self._assistant_parts: list[str] = []
        self._reasoning_announced = False
        self._turn_worker: Worker[None] | None = None
        self._announced_terminal_tasks: set[str] = set()
        self._task_polling = False

    @property
    def entries(self) -> tuple[TranscriptEntry, ...]:
        return tuple(self._entries)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield RichLog(id="transcript", wrap=True, highlight=False, markup=False)
        yield Static("", id="stream")
        yield Input(placeholder="Ask Neuro Code… (/help for commands)", id="prompt")
        yield Footer()

    def on_mount(self) -> None:
        if self._approval_controller is not None:
            self._approval_controller.set_handler(self._request_approval)
        if self._runner.session_id is not None:
            self._replace_transcript(self._initial_items)
            self._write_entry(
                "system",
                f"Resumed session {self._runner.session_id or 'unknown'} · "
                f"{self._provider_name}/{self._model_name} · {self._cwd}",
            )
        else:
            self._write_entry(
                "system",
                f"Ready · {self._provider_name}/{self._model_name} · {self._cwd}",
            )
        if self._task_controller is not None:
            self.set_interval(_TASK_POLL_SECONDS, self._poll_background_tasks)
        self.query_one("#prompt", Input).focus()

    def on_unmount(self) -> None:
        if self._approval_controller is not None:
            self._approval_controller.set_handler(None)

    async def _request_approval(self, request: PermissionRequest) -> PermissionApproval:
        return await self.push_screen_wait(PermissionApprovalScreen(request))

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        prompt = event.value.strip()
        event.input.value = ""
        if not prompt:
            return
        if prompt.startswith("/"):
            await self._dispatch_slash_command(prompt)
            return
        if self._turn_worker is not None and self._turn_worker.is_running:
            self._write_entry("error", "A turn is already running.")
            return

        self._write_entry("user", prompt)
        self._assistant_parts.clear()
        self._reasoning_announced = False
        self.query_one("#stream", Static).update(Text("Assistant: …", style="italic"))
        self._turn_worker = self.run_worker(
            self._run_prompt(prompt),
            name="agent-turn",
            group="agent",
            exclusive=True,
            exit_on_error=False,
        )

    async def _run_prompt(self, prompt: str) -> None:
        prompt_input = self.query_one("#prompt", Input)
        stream = self.query_one("#stream", Static)
        try:
            result = await self._runner.run(prompt, sink=self._handle_event)
            response = result.response or "(no text response)"
            self._write_entry("assistant", response)
        except asyncio.CancelledError:
            self._write_entry("status", "Turn cancelled.")
            raise
        except Exception as error:
            self._write_entry("error", f"{type(error).__name__}: {error}")
        finally:
            stream.update("")
            prompt_input.focus()

    async def _handle_event(self, event: AgentEvent) -> None:
        data = event.data
        if event.kind is AgentEventKind.TEXT_DELTA:
            text = data.get("text")
            if isinstance(text, str):
                self._assistant_parts.append(text)
                rendered = Text("Assistant: ", style="bold cyan")
                rendered.append("".join(self._assistant_parts))
                self.query_one("#stream", Static).update(rendered)
        elif event.kind is AgentEventKind.REASONING_DELTA and not self._reasoning_announced:
            self._reasoning_announced = True
            self._write_entry("status", "Reasoning…")
        elif event.kind is AgentEventKind.PROVIDER_ATTEMPT_FAILED:
            provider = self._field(data, "provider")
            message = self._field(data, "message")
            self._write_entry("error", f"Provider {provider} failed before output: {message}")
        elif event.kind is AgentEventKind.PROVIDER_SELECTED:
            provider = self._field(data, "provider")
            model = self._field(data, "model")
            self._provider_name = provider
            self._model_name = model
            qualifier = "fallback selected" if data.get("failover") is True else "selected"
            self._write_entry("status", f"Provider {provider}/{model} {qualifier}.")
        elif event.kind is AgentEventKind.BACKEND_TOOL_STARTED:
            self._write_entry("tool", f"Hosted tool {self._field(data, 'name')} started.")
        elif event.kind is AgentEventKind.BACKEND_TOOL_COMPLETED:
            self._write_entry("tool", f"Hosted tool {self._field(data, 'name')} completed.")
        elif event.kind is AgentEventKind.TOOL_REQUESTED:
            self._write_entry("tool", f"Tool {self._field(data, 'name')} requested.")
        elif event.kind is AgentEventKind.TOOL_PERMISSION:
            effect = self._field(data, "effect")
            if effect == "deny":
                self._write_entry(
                    "error",
                    f"Tool {self._field(data, 'name')} permission: {effect} "
                    f"({self._field(data, 'reason')}).",
                )
        elif event.kind is AgentEventKind.TOOL_APPROVAL_REQUESTED:
            self._write_entry(
                "status",
                f"Tool {self._field(data, 'name')} is waiting for approval.",
            )
        elif event.kind is AgentEventKind.TOOL_APPROVAL_RESOLVED:
            effect = self._field(data, "effect")
            outcome = self._field(data, "outcome")
            category = "status" if effect == "allow" else "error"
            self._write_entry(
                category,
                f"Tool {self._field(data, 'name')} approval resolved: {outcome}.",
            )
        elif event.kind is AgentEventKind.TOOL_COMPLETED:
            self._write_entry("tool", f"Tool {self._field(data, 'name')} completed.")
        elif event.kind is AgentEventKind.TOOL_FAILED:
            self._write_entry("error", f"Tool {self._field(data, 'name')} failed.")

    @staticmethod
    def _field(data: Mapping[str, Any], name: str) -> str:
        value = data.get(name)
        if isinstance(value, str) and value:
            return value
        return "unknown"

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
                self._write_entry("error", "/tasks does not accept arguments.")
                return
            await self._show_background_tasks()
            return
        if arguments.strip():
            self._write_entry("error", f"/{command} does not accept arguments.")
            return
        if command in {"quit", "exit"}:
            self.exit()
        elif command == "cancel":
            self.action_cancel_turn()
        elif command == "clear":
            self.action_clear_transcript()
        elif command == "help":
            self._write_entry(
                "system",
                "Commands: /help, /status, /provider [PROFILE] (alias /model), "
                "/sessions [QUERY], /resume [SESSION_ID], /rename TITLE (alias /title), "
                "/tasks, /cancel, /clear, /quit (alias /exit).",
            )
        elif command == "status":
            session_id = self._runner.session_id or "not created"
            profile = (
                f" · Profile: {self._provider_controller.selected_profile}"
                if self._provider_controller is not None
                else ""
            )
            self._write_entry(
                "system",
                f"Provider: {self._provider_name}/{self._model_name} · "
                f"Session: {session_id}{profile} · CWD: {self._cwd}",
            )
        else:
            self._write_entry("error", f"Unknown command: /{command}. Use /help.")

    def action_clear_transcript(self) -> None:
        self.query_one("#transcript", RichLog).clear()
        self._entries.clear()
        self._write_entry("system", "Transcript cleared.")

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
        if self._turn_worker is not None and self._turn_worker.is_running:
            self._write_entry("status", "Cancellation requested.")
            self._turn_worker.cancel()
            return
        prompt = self.query_one("#prompt", Input)
        if prompt.value:
            prompt.value = ""
            self._write_entry("status", "Draft cleared.")
        else:
            self._write_entry("status", "No turn is running.")

    async def action_select_provider(self) -> None:
        await self._select_provider(None)

    async def action_select_session(self) -> None:
        await self._select_session(None)

    async def _select_provider(self, requested: str | None) -> None:
        if self._provider_controller is None:
            self._write_entry("error", "Provider switching is unavailable.")
            return
        if self._turn_worker is not None and self._turn_worker.is_running:
            self._write_entry("error", "Cannot switch provider while a turn is running.")
            return
        profile_name = requested
        if profile_name is None:
            self.push_screen(
                ProviderSelectionScreen(self._provider_controller.profiles),
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
            self._write_entry(
                "status",
                f"Provider profile {result.profile_name} is already selected.",
            )
        elif result.previous_session_id is None:
            self._write_entry(
                "status",
                f"Provider profile switched to {result.profile_name} "
                f"({result.provider_name}/{result.model_name})."
                f"{self._stopped_task_note(result.stopped_background_tasks)}",
            )
        else:
            self._write_entry(
                "status",
                f"Provider profile switched to {result.profile_name} "
                f"({result.provider_name}/{result.model_name}). The previous session "
                f"{result.previous_session_id} remains saved; the next prompt starts "
                f"a new conversation.{self._stopped_task_note(result.stopped_background_tasks)}",
            )

    async def _select_session(
        self,
        requested: str | None,
        *,
        query: str | None = None,
    ) -> None:
        if self._session_controller is None:
            self._write_entry("error", "Interactive session resume is unavailable.")
            return
        if self._turn_worker is not None and self._turn_worker.is_running:
            self._write_entry("error", "Cannot resume a session while a turn is running.")
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
            qualifier = f" matching {query!r}" if query is not None else ""
            self._write_entry(
                "status",
                f"No sessions found for this workspace{qualifier}.",
            )
            return
        self.push_screen(
            SessionSelectionScreen(options, query=query),
            self._session_selected,
        )

    async def _rename_session(self, title: str) -> None:
        if self._session_controller is None:
            self._write_entry("error", "Interactive session rename is unavailable.")
            return
        if self._turn_worker is not None and self._turn_worker.is_running:
            self._write_entry("error", "Cannot rename a session while a turn is running.")
            return
        try:
            summary = await self._session_controller.rename_session(title)
        except Exception as error:
            self._write_entry("error", f"{type(error).__name__}: {error}")
            return
        self._write_entry(
            "status",
            f"Session {summary.id} renamed to {summary.title!r}.",
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
            self._write_entry("status", f"Session {result.session_id} is already open.")
            return

        self._reset_background_task_tracking()
        self._replace_transcript(result.items)
        profile_note = (
            f"profile {result.profile_name}"
            if result.source_profile_match
            else (
                f"profile {result.profile_name}; source profile "
                f"{result.source_provider} is not ready locally"
            )
        )
        previous_note = (
            f" Previous session {result.previous_session_id} remains saved."
            if result.previous_session_id is not None
            else ""
        )
        self._write_entry(
            "system",
            f"Resumed session {result.session_id} with {profile_note} "
            f"({result.provider_name}/{result.model_name}).{previous_note}"
            f"{self._stopped_task_note(result.stopped_background_tasks)}",
        )

    async def _show_background_tasks(self) -> None:
        if self._task_controller is None:
            self._write_entry("error", "Background task visibility is unavailable.")
            return
        try:
            snapshots = await self._task_controller.list_background_tasks()
        except Exception as error:
            self._write_entry("error", f"{type(error).__name__}: {error}")
            return
        if not snapshots:
            self._write_entry("status", "No background tasks for the current session.")
            return

        visible = snapshots[-_TASK_LIST_LIMIT:]
        omitted = len(snapshots) - len(visible)
        lines = [self._task_summary(snapshot) for snapshot in visible]
        if omitted:
            lines.insert(0, f"… {omitted} older task(s) omitted")
        self._write_entry("system", "Background tasks:\n" + "\n".join(lines))

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

    @staticmethod
    def _task_summary(snapshot: BackgroundTaskSnapshot) -> str:
        exit_note = f" · exit {snapshot.exit_code}" if snapshot.exit_code is not None else ""
        truncation_note = " (preview truncated)" if snapshot.truncated else ""
        started = snapshot.started_at.astimezone().strftime("%H:%M:%S")
        return (
            f"{snapshot.task_id} · {snapshot.status.value}{exit_note} · "
            f"{snapshot.total_output_bytes} output bytes{truncation_note} · started {started}"
        )

    @staticmethod
    def _task_completion_message(snapshot: BackgroundTaskSnapshot) -> str:
        descriptions = {
            BackgroundTaskStatus.COMPLETED: "completed",
            BackgroundTaskStatus.FAILED: "failed",
            BackgroundTaskStatus.TIMED_OUT: "timed out",
            BackgroundTaskStatus.CANCELLED: "was cancelled",
        }
        description = descriptions.get(snapshot.status, snapshot.status.value)
        exit_note = f" (exit {snapshot.exit_code})" if snapshot.exit_code is not None else ""
        return f"Background task {snapshot.task_id} {description}{exit_note}."

    @staticmethod
    def _stopped_task_note(count: int) -> str:
        if count == 0:
            return ""
        noun = "task" if count == 1 else "tasks"
        return f" Stopped {count} background {noun} from the previous session."

    def _replace_transcript(self, items: Sequence[SessionItem]) -> None:
        self.query_one("#transcript", RichLog).clear()
        self._entries.clear()
        for item in items:
            if not isinstance(item, Message) or item.role is Role.SYSTEM:
                continue
            if item.role is Role.TOOL:
                self._write_entry("tool", f"Restored result for {item.name or 'unknown'}.")
                continue
            content = self._bounded_restored_text(item.model_content())
            if content:
                category = "user" if item.role is Role.USER else "assistant"
                self._write_entry(category, content)
            if item.role is Role.ASSISTANT and item.tool_calls:
                names = ", ".join(call.name for call in item.tool_calls)
                self._write_entry("tool", f"Restored tool request: {names}.")

    @staticmethod
    def _bounded_restored_text(content: str) -> str:
        if len(content) <= _RESTORED_MESSAGE_LIMIT:
            return content
        return f"{content[:_RESTORED_MESSAGE_LIMIT]}\n… [restored message truncated]"

    def _write_entry(self, category: str, content: str) -> None:
        entry = TranscriptEntry(category, content)
        self._entries.append(entry)
        labels = {
            "assistant": ("Assistant", "bold cyan"),
            "error": ("Error", "bold red"),
            "status": ("Status", "dim"),
            "system": ("Neuro Code", "bold magenta"),
            "tool": ("Tool", "bold yellow"),
            "user": ("You", "bold green"),
        }
        label, style = labels.get(category, (category.title(), ""))
        rendered = Text(f"{label}: ", style=style)
        rendered.append(content)
        self.query_one("#transcript", RichLog).write(rendered)


__all__ = [
    "ApprovalController",
    "ConversationRunner",
    "NeuroCodeApp",
    "PermissionApprovalScreen",
    "ProviderController",
    "ProviderSelectionScreen",
    "SessionController",
    "SessionSelectionScreen",
    "TaskController",
    "TranscriptEntry",
]
