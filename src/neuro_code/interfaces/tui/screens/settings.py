"""Settings screens for user-owned TUI preferences.

TUI 用户偏好设置屏幕.
"""

from __future__ import annotations

import os
from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static

from neuro_code.application.ports.configuration import resolve_http_client_policy
from neuro_code.application.ports.provider_settings import (
    ManagedProviderSettings,
    ManagedProxyPolicy,
    ProviderSettingsStore,
)
from neuro_code.domain.background_tasks.models import BackgroundTaskWakePolicy
from neuro_code.interfaces.tui.state import _ERROR_MARK
from neuro_code.interfaces.tui.text import language_name, ui_text
from neuro_code.interfaces.tui.theme import ERROR_TEXT_STYLE
from neuro_code.interfaces.tui.widgets import MenuOptionButton
from neuro_code.shared.ui_language import UiLanguage


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
        max-width: 88;
        height: auto;
        max-height: 85%;
        padding: $space-2 $space-3;
        border: solid $border;
        background: $surface;
    }

    #settings-title {
        text-style: bold;
        color: $text-primary;
        margin-bottom: $space-1;
    }

    #settings-description {
        color: $text-muted;
        margin-bottom: $space-1;
    }

    #settings-categories {
        height: auto;
    }

    #settings-categories MenuOptionButton {
        width: 100%;
        height: 3;
        margin-bottom: $space-0;
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
                MenuOptionButton(
                    ui_text(self.language, "settings.category.language.label"),
                    secondary=language_summary,
                    id="settings-category-language",
                ),
                MenuOptionButton(
                    ui_text(self.language, "settings.category.providers.label"),
                    secondary=ui_text(self.language, "settings.category.providers.value"),
                    id="settings-category-providers",
                    disabled=not self.provider_settings_available,
                ),
                MenuOptionButton(
                    ui_text(self.language, "settings.category.network.label"),
                    secondary=ui_text(self.language, "settings.category.network.value"),
                    id="settings-category-network",
                    disabled=not self.provider_settings_available,
                ),
                MenuOptionButton(
                    ui_text(self.language, "settings.category.background_wake.label"),
                    secondary=ui_text(
                        self.language,
                        "settings.category.background_wake.value",
                    ),
                    id="settings-category-background-wake",
                    disabled=not self.provider_settings_available,
                ),
                id="settings-categories",
            ),
            Static(ui_text(self.language, "settings.help"), id="settings-help"),
            id="settings-dialog",
            classes="modal-dialog modal-m",
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
        width: 76%;
        max-width: 72;
        height: auto;
        padding: $space-2 $space-3;
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

    #settings-languages MenuOptionButton {
        width: 100%;
        height: 3;
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
            Vertical(
                MenuOptionButton(
                    language_name(
                        UiLanguage.SIMPLIFIED_CHINESE,
                        in_language=UiLanguage.SIMPLIFIED_CHINESE,
                    ),
                    id="settings-language-zh-cn",
                    selected=self.selected is UiLanguage.SIMPLIFIED_CHINESE,
                ),
                MenuOptionButton(
                    language_name(UiLanguage.ENGLISH, in_language=UiLanguage.ENGLISH),
                    id="settings-language-en",
                    selected=self.selected is UiLanguage.ENGLISH,
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
            classes="modal-dialog modal-s",
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
        padding: $space-2 $space-3;
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
        socks_supported: bool = False,
    ) -> None:
        super().__init__()
        self.language = language
        self.provider_settings = provider_settings
        self.provider_settings_store = provider_settings_store
        self.socks_supported = socks_supported
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
            classes="modal-dialog modal-m",
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
                socks_supported=self.socks_supported,
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
        padding: $space-2 $space-3;
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
            classes="modal-dialog modal-m",
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


__all__ = [
    "BackgroundWakeSettingsScreen",
    "LanguageSettingsScreen",
    "NetworkProxySettingsScreen",
    "SettingsScreen",
]
