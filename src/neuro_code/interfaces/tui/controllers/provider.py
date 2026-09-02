from __future__ import annotations

from neuro_code.application.providers.service import ChangeProviderRequest
from neuro_code.domain.background_tasks.models import (
    BackgroundTaskWakePolicy,
)
from neuro_code.interfaces.tui.controllers.base import TuiAppControllerMixin
from neuro_code.interfaces.tui.screens import ProviderSelectionScreen


class ProviderControllerMixin(TuiAppControllerMixin):
    async def action_select_provider(self) -> None:
        await self._select_provider(None)

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
