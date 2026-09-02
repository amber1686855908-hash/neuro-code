from __future__ import annotations

from textual.screen import ModalScreen

from neuro_code.application.ports.provider_settings import (
    ManagedProviderSettings,
)
from neuro_code.domain.conversation.interaction_mode import InteractionMode
from neuro_code.domain.conversation.reasoning import ReasoningEffort
from neuro_code.interfaces.tui.controllers.base import TuiAppControllerMixin
from neuro_code.interfaces.tui.screens import (
    BackgroundWakeSettingsScreen,
    LanguageSettingsScreen,
    NetworkProxySettingsScreen,
    ProviderSettingsScreen,
    ReasoningEffortScreen,
    SettingsScreen,
)
from neuro_code.interfaces.tui.state import TUI_RELOAD_PROVIDER_SETTINGS, ProviderSettingsSubmission
from neuro_code.interfaces.tui.text import language_name
from neuro_code.shared.ui_language import UiLanguage


class PreferencesControllerMixin(TuiAppControllerMixin):
    async def action_select_reasoning_effort(self) -> None:
        await self._select_reasoning_effort(None)

    async def action_cycle_interaction_mode(self) -> None:
        if isinstance(self.screen, ModalScreen):
            self.screen.focus_previous()
            return
        await self._apply_interaction_mode(self._interaction_mode.next)

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
                    socks_supported=self._socks_supported,
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
                    socks_supported=self._socks_supported,
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
                "effort.changed_ultracode",
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
