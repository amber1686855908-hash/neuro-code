"""Persistence actions for the Provider settings screen.

Provider 设置屏幕的持久化操作.

The screen continues to use the injected ``ProviderSettingsStore``.  This
module only owns save/delete orchestration and the confirmation state around
those operations; storage remains an application port implementation.
"""

from __future__ import annotations

import os

from textual.widgets import Button, Input

from neuro_code.application.ports.configuration import resolve_http_client_policy
from neuro_code.application.ports.model import ModelCapabilitySet
from neuro_code.application.ports.provider_services import ProtocolSupportStatus
from neuro_code.application.ports.provider_settings import ManagedProviderProfile
from neuro_code.interfaces.tui.screens.provider_context import ProviderSettingsScreenMixin
from neuro_code.interfaces.tui.state import ProviderSettingsSubmission
from neuro_code.interfaces.tui.text import ui_text


class ProviderPersistenceMixin(ProviderSettingsScreenMixin):
    """Own Provider profile save, delete, and confirmation workflows."""

    async def _save_provider(self) -> None:
        service = self._service(self._active_preset)
        if service is None:
            self._show_provider_error("provider service selection is unavailable")
            return
        api_key = self.query_one("#provider-settings-api-key", Input).value.strip() or None
        name = self.query_one("#provider-settings-name", Input).value.strip()
        base_url = self.query_one("#provider-settings-base-url", Input).value.strip()
        model = self.query_one("#provider-settings-model", Input).value.strip()
        try:
            protocol_status = service.protocol_support_for(
                model=model,
                protocol=self._active_protocol,
            )
            if protocol_status is ProtocolSupportStatus.UNSUPPORTED:
                raise ValueError(
                    f"{service.display_name} does not document {self._active_protocol} "
                    f"for model {model!r}"
                )
            existing = self.provider_settings.profile(name)
            proxy_policy = self._draft_proxy_policy()
            profile = ManagedProviderProfile(
                name=name,
                protocol=self._active_protocol,
                dialect=service.dialect_for(self._active_protocol),
                service_id=service.service_id,
                capability_overrides=(
                    existing.capability_overrides
                    if existing is not None
                    else ModelCapabilitySet.all_unknown()
                ),
                model=model,
                base_url=base_url,
                context_window_tokens=self._context_window_tokens(),
                proxy_mode=self._active_proxy_mode,
                proxy_url_env=(
                    proxy_policy.proxy_url_env if self._active_proxy_mode is not None else None
                ),
                api_key=api_key,
                background_task_wake_policy=self._active_background_wake_policy,
            )
            if existing is None and api_key is None:
                raise ValueError(ui_text(self.language, "provider_settings.api_key_required"))
            resolve_http_client_policy(
                proxy_mode=proxy_policy.mode,
                proxy_url_env=proxy_policy.proxy_url_env,
                environ=os.environ,
                socks_supported=self.socks_supported,
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


__all__ = ["ProviderPersistenceMixin"]
