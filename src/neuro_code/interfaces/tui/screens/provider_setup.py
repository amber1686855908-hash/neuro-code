"""Standalone lifecycle owner for the Provider setup application.

Provider 设置应用的独立生命周期 owner.

``ProviderSetupApp`` owns only the focused Textual application lifecycle.  The
editable Provider screen and its interaction concerns remain in
``provider_screen`` and its focused mixins.
"""

from __future__ import annotations

from textual.app import App

from neuro_code.application.ports.provider_catalog import ProviderCatalog
from neuro_code.application.ports.provider_settings import (
    ManagedProviderSettings,
    ProviderSettingsStore,
)
from neuro_code.interfaces.tui.screens.provider_screen import ProviderSettingsScreen
from neuro_code.interfaces.tui.state import ProviderSettingsSubmission
from neuro_code.interfaces.tui.theme import TEXTUAL_THEME
from neuro_code.shared.ui_language import UiLanguage


class ProviderSetupApp(App[bool]):
    """Focused provider setup used for first run and recoverable startup errors.

    用于首次运行和可恢复启动错误的聚焦 Provider 配置界面.
    """

    CSS = """
    Screen {
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
        socks_supported: bool = False,
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
        self._socks_supported = socks_supported
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
                socks_supported=self._socks_supported,
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


__all__ = ["ProviderSetupApp"]
