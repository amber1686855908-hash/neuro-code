"""Selection and permission screens for the TUI.

TUI 选择与权限屏幕.
"""

from __future__ import annotations

import asyncio
from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Button, Input, Label, Static

from neuro_code.application.permissions.contracts import PermissionApproval, PermissionRequest
from neuro_code.application.permissions.scopes import PermissionScopeCandidate, PermissionScopeKind
from neuro_code.application.providers.contracts import ProviderOption
from neuro_code.application.sessions.contracts import SessionOption
from neuro_code.domain.conversation.reasoning import ReasoningEffort
from neuro_code.domain.sandbox.models import SandboxProfile
from neuro_code.interfaces.tui.contracts import SessionSearchCallback
from neuro_code.interfaces.tui.text import ui_text
from neuro_code.interfaces.tui.widgets import MenuOptionButton
from neuro_code.shared.ui_language import UiLanguage


class ReasoningEffortScreen(ModalScreen[ReasoningEffort | None]):
    """Select application-owned review depth without implying native API support.

    选择应用层拥有的审查深度,不暗示底层 API 原生支持."""

    CSS = """
    ReasoningEffortScreen {
        align: center middle;
        background: $background 85%;
    }

    #effort-dialog {
        width: 82%;
        max-width: 88;
        height: auto;
        max-height: 90%;
        padding: $space-2 $space-3;
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

    #effort-options MenuOptionButton {
        width: 100%;
        height: 3;
        margin-bottom: $space-0;
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

    def compose(self) -> ComposeResult:
        buttons = [
            MenuOptionButton(
                effort.value,
                secondary=(
                    f"{ui_text(self.language, f'effort.description.{effort.value}')} · "
                    f"{ui_text(self.language, 'effort.workflow_delegation')}"
                    if effort is ReasoningEffort.ULTRACODE
                    else ui_text(self.language, f"effort.description.{effort.value}")
                ),
                selected=effort is self.selected,
                muted=False,
                primary_width=14,
                secondary_justify="left",
                id=f"effort-choice-{index}",
            )
            for index, effort in enumerate(ReasoningEffort)
        ]
        yield Vertical(
            Label(ui_text(self.language, "effort.title"), id="effort-title"),
            VerticalScroll(*buttons, id="effort-options"),
            Static(ui_text(self.language, "effort.help"), id="effort-help"),
            id="effort-dialog",
            classes="modal-dialog modal-m",
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
        width: 82%;
        max-width: 88;
        height: auto;
        max-height: 90%;
        padding: $space-2 $space-3;
        border: solid $border;
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
        margin: $space-1 $space-0;
        padding: $space-1;
        border: none;
        background: $background;
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
        self._scope_button_candidates: dict[str, PermissionScopeCandidate] = {}

    def compose(self) -> ComposeResult:
        scope_widgets: list[Widget] = []
        if self.request.scope_candidates:
            scope_widgets.append(
                Static(
                    Text(ui_text(self.language, "approval.scope.heading")),
                    id="approval-scope-heading",
                )
            )
            for candidate in self.request.scope_candidates:
                if candidate.kind is PermissionScopeKind.WORKSPACE_EDITS:
                    button_id = "approval-allow-scope-workspace-edits"
                    label_key = "approval.scope.workspace_edits"
                    label_kwargs = {"root": candidate.workspace_root or "(unknown)"}
                elif candidate.kind is PermissionScopeKind.COMMAND_FAMILY:
                    family = (
                        candidate.command_family.value if candidate.command_family else "(unknown)"
                    )
                    button_id = f"approval-allow-scope-command-family-{family}"
                    label_key = "approval.scope.command_family"
                    label_kwargs = {
                        "family": family,
                        "root": candidate.workspace_root or "(unknown)",
                    }
                else:
                    continue
                self._scope_button_candidates[button_id] = candidate
                scope_widgets.append(
                    Button(
                        ui_text(self.language, label_key, **label_kwargs),
                        variant="primary",
                        id=button_id,
                    )
                )
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
            *scope_widgets,
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
            classes="modal-dialog modal-m",
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
        if approval is None:
            candidate = self._scope_button_candidates.get(event.button.id or "")
            if candidate is not None:
                approval = PermissionApproval.allow_scope(candidate)
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
        width: 92%;
        max-width: 116;
        height: 80%;
        padding: $space-2 $space-3;
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
            classes="modal-dialog modal-l",
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
        width: 92%;
        max-width: 116;
        height: 80%;
        padding: $space-2 $space-3;
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
        self._initial_query_pending = query is not None
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
            sandbox_key = (
                "session.sandbox_off"
                if option.sandbox_profile is SandboxProfile.OFF
                else "session.sandbox"
            )
            markers.append(
                ui_text(
                    language,
                    sandbox_key,
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
            classes="modal-dialog modal-l",
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
        normalized_query = event.value.strip() or None
        if self._initial_query_pending:
            self._initial_query_pending = False
            if normalized_query == self.search_query:
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


__all__ = [
    "PermissionApprovalScreen",
    "ProviderSelectionScreen",
    "ReasoningEffortScreen",
    "SessionSelectionScreen",
]
