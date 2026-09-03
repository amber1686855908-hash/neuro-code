"""Interaction and selection handlers for the Provider settings screen.

Provider 设置屏幕的交互与选择处理器.

This module owns Textual button/input events and the screen-local transitions
for service, endpoint, protocol, profile, proxy, and background-wake choices.
It delegates draft construction, discovery, and persistence to their focused
mixins.
"""

from __future__ import annotations

from textual.containers import Horizontal
from textual.widgets import Button, Input, Static

from neuro_code.application.ports.provider_services import (
    DEFAULT_PROVIDER_SERVICE_CATALOG,
    ProtocolSupportStatus,
    ProviderServiceCatalog,
    ProviderServiceDescriptor,
)
from neuro_code.application.ports.provider_settings import ManagedProviderProfile
from neuro_code.domain.background_tasks.models import BackgroundTaskWakePolicy
from neuro_code.interfaces.tui.screens.provider_context import ProviderSettingsScreenMixin
from neuro_code.interfaces.tui.text import ui_text


class ProviderInteractionMixin(ProviderSettingsScreenMixin):
    """Own Provider settings Textual events and selection state transitions."""

    def _service(self, identifier: str) -> ProviderServiceDescriptor | None:
        return self.service_catalog.get(identifier)

    def _active_service(self) -> ProviderServiceDescriptor | None:
        return self._service(self._active_preset)

    def _refresh_endpoint_controls(self, service: ProviderServiceDescriptor) -> None:
        container = self.query_one("#provider-settings-endpoints", Horizontal)
        available = {variant.variant_id for variant in service.endpoint_variants}
        container.display = bool(available)
        for button in container.query(Button):
            variant_id = (button.id or "").removeprefix("provider-settings-endpoint-")
            button.display = variant_id in available
            button.variant = "primary" if variant_id == self._active_endpoint_variant else "default"

    def _refresh_protocol_controls(self, service: ProviderServiceDescriptor) -> None:
        for button in self.query_one("#provider-settings-protocols", Horizontal).query(Button):
            protocol = (button.id or "").removeprefix("provider-settings-protocol-")
            if protocol == self._RECOMMENDED_PROTOCOL:
                button.display = bool(service.supported_protocols)
                button.disabled = not bool(service.supported_protocols)
                button.label = self._protocol_label(protocol)
                button.variant = "primary" if self._protocol_auto else "default"
                continue
            available = protocol in service.supported_protocols
            status = self._model_protocol_status(service, protocol)
            button.display = available
            button.disabled = not available or status is ProtocolSupportStatus.UNSUPPORTED
            label = self._protocol_label(protocol)
            if (
                status is ProtocolSupportStatus.UNKNOWN
                and self.query_one("#provider-settings-model", Input).value.strip()
            ):
                label = f"? {label}"
            button.label = label
            button.variant = (
                "primary"
                if not self._protocol_auto and protocol == self._active_protocol
                else "default"
            )

    def _set_endpoint_url(self, value: str) -> None:
        self._updating_endpoint = True
        try:
            self.query_one("#provider-settings-base-url", Input).value = value
        finally:
            self._updating_endpoint = False

    def _refresh_provider_controls(self, service: ProviderServiceDescriptor) -> None:
        self._refresh_endpoint_controls(service)
        self._refresh_protocol_controls(service)
        hint = service.protocol_hint_for(self._active_protocol)
        self.query_one("#provider-settings-protocol-hint", Static).update(
            self._service_text(
                hint,
                f"{service.display_name} · {self._active_protocol}",
            )
        )

    def _select_endpoint_variant(self, variant_id: str) -> None:
        service = self._active_service()
        if service is None or service.endpoint_variant_for(variant_id) is None:
            return
        self._active_endpoint_variant = variant_id
        if self._endpoint_url_managed:
            self._set_endpoint_url(
                service.endpoint_for(protocol=self._active_protocol, variant_id=variant_id)
            )
        self._clear_model_catalog()
        self._refresh_endpoint_controls(service)

    def _select_protocol(self, protocol: str) -> None:
        service = self._active_service()
        if protocol == self._RECOMMENDED_PROTOCOL:
            if service is None or not service.supported_protocols:
                return
            self._protocol_auto = True
            protocol = self._recommended_protocol(service)
        else:
            self._protocol_auto = False
        if service is None or protocol not in service.supported_protocols:
            return
        status = self._model_protocol_status(service, protocol)
        if status is ProtocolSupportStatus.UNSUPPORTED:
            self._show_provider_error(
                f"{service.display_name} does not document {protocol} for the selected model"
            )
            return
        self._active_protocol = protocol
        if self._endpoint_url_managed:
            self._set_endpoint_url(
                service.endpoint_for(
                    protocol=protocol,
                    variant_id=self._active_endpoint_variant,
                )
            )
        self._clear_model_catalog()
        self._refresh_provider_controls(service)
        if status is ProtocolSupportStatus.UNKNOWN:
            self._show_provider_error(ui_text(self.language, "provider_settings.protocol.unknown"))

    def on_mount(self) -> None:
        default_service = self._default_service()
        self._refresh_provider_controls(default_service)
        if self.initial_profile is not None:
            self._edit_profile(self.initial_profile)
        if self.initial_error:
            self._show_provider_error(self.initial_error)
        focus_target = (
            "#provider-settings-model"
            if self._editing_profile is not None
            else f"#provider-settings-preset-{self._active_preset}"
        )
        self.query_one(focus_target).focus()

    def _default_service(self) -> ProviderServiceDescriptor:
        return self.service_catalog.services[0]

    def _default_service_key(self) -> str:
        service = self._default_service()
        return service.ui_key or service.service_id

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        profile_name = self._profile_ids.get(button_id)
        if profile_name is not None:
            self._edit_profile(profile_name)
            return
        catalog_model = self._catalog_model_ids.get(button_id)
        if catalog_model is not None:
            self.query_one("#provider-settings-model", Input).value = catalog_model
            self._show_connection_status(
                ui_text(
                    self.language,
                    "provider_settings.connection.selected",
                    model=catalog_model,
                ),
                kind="success",
            )
            return
        if button_id.startswith("provider-settings-preset-"):
            self._select_preset(
                button_id.removeprefix("provider-settings-preset-"),
                clear_model=True,
            )
            return
        if button_id.startswith("provider-settings-endpoint-"):
            self._select_endpoint_variant(button_id.removeprefix("provider-settings-endpoint-"))
            return
        if button_id.startswith("provider-settings-protocol-"):
            self._select_protocol(button_id.removeprefix("provider-settings-protocol-"))
            return
        if button_id.startswith("provider-settings-proxy-"):
            selection = button_id.removeprefix("provider-settings-proxy-")
            self._select_proxy_mode(None if selection == "inherit" else selection)
            return
        if button_id.startswith("provider-settings-wake-"):
            selection = button_id.removeprefix("provider-settings-wake-")
            self._select_background_wake_policy(
                None if selection == "inherit" else BackgroundTaskWakePolicy(selection)
            )
            return
        if button_id == "provider-settings-new":
            self._new_profile()
            return
        if button_id == "provider-settings-save":
            await self._save_provider()
            return
        if button_id == "provider-settings-test":
            self.run_worker(
                self._test_connection(),
                name="provider-model-discovery",
                group="provider-model-discovery",
                exclusive=True,
                exit_on_error=False,
            )
            return
        if button_id == "provider-settings-delete":
            await self._delete_provider()
            return
        if button_id == "provider-settings-back":
            self.dismiss(None)

    def _edit_profile(self, name: str) -> None:
        profile = self.provider_settings.profile(name)
        if profile is None:
            return
        self._editing_profile = name
        self._clear_model_catalog()
        name_input = self.query_one("#provider-settings-name", Input)
        name_input.value = profile.name
        name_input.disabled = True
        self.query_one("#provider-settings-model", Input).value = profile.model
        self.query_one("#provider-settings-base-url", Input).value = profile.base_url
        self.query_one("#provider-settings-api-key", Input).value = ""
        self.query_one("#provider-settings-context-window", Input).value = (
            str(profile.context_window_tokens) if profile.context_window_tokens is not None else ""
        )
        service = self.service_catalog.match_profile(
            service_id=profile.service_id,
            protocol=profile.protocol,
            dialect=profile.dialect,
            base_url=profile.base_url,
        )
        self._endpoint_url_managed = False
        self._protocol_auto = False
        self._select_preset(
            self._preset_for_profile(profile, self.service_catalog),
            update_endpoint=False,
        )
        self._active_protocol = profile.protocol
        self._active_endpoint_variant = None
        if service is not None:
            normalized_base_url = profile.base_url.rstrip("/").casefold()
            self._active_endpoint_variant = next(
                (
                    variant.variant_id
                    for variant in service.endpoint_variants
                    if (variant.base_url_for(profile.protocol) or "").rstrip("/").casefold()
                    == normalized_base_url
                ),
                None,
            )
            self._refresh_provider_controls(service)
        self.query_one("#provider-settings-proxy-env", Input).value = profile.proxy_url_env or ""
        self._select_proxy_mode(profile.proxy_mode)
        self._select_background_wake_policy(profile.background_task_wake_policy)
        self._reset_delete_confirmation()
        if not self.first_run:
            self.query_one("#provider-settings-delete", Button).disabled = False
        self._show_provider_error("")
        self.query_one("#provider-settings-model", Input).focus()

    @staticmethod
    def _preset_for_profile(
        profile: ManagedProviderProfile,
        service_catalog: ProviderServiceCatalog = DEFAULT_PROVIDER_SERVICE_CATALOG,
    ) -> str:
        service = service_catalog.match_profile(
            service_id=profile.service_id,
            protocol=profile.protocol,
            dialect=profile.dialect,
            base_url=profile.base_url,
        )
        if service is None:
            service = service_catalog.services[0]
        return service.ui_key or service.service_id

    def _new_profile(self) -> None:
        self._editing_profile = None
        self._clear_model_catalog()
        self._endpoint_url_managed = True
        name_input = self.query_one("#provider-settings-name", Input)
        name_input.disabled = False
        name_input.value = ""
        self.query_one("#provider-settings-model", Input).value = ""
        self.query_one("#provider-settings-api-key", Input).value = ""
        self.query_one("#provider-settings-context-window", Input).value = ""
        self.query_one("#provider-settings-proxy-env", Input).value = ""
        self._select_preset(self._default_service_key())
        self._select_proxy_mode(None)
        self._select_background_wake_policy(None)
        self._reset_delete_confirmation()
        if not self.first_run:
            self.query_one("#provider-settings-delete", Button).disabled = True
        self._show_provider_error("")
        name_input.focus()

    def _select_preset(
        self,
        preset_name: str,
        *,
        update_endpoint: bool = True,
        clear_model: bool = False,
    ) -> None:
        service = self._service(preset_name)
        if service is None:
            return
        self._clear_model_catalog()
        self._active_preset = service.ui_key or service.service_id
        self._active_protocol = service.default_protocol
        self._protocol_auto = False
        self._active_endpoint_variant = (
            service.default_endpoint_variant.variant_id
            if service.default_endpoint_variant is not None
            else None
        )
        if clear_model:
            self.query_one("#provider-settings-model", Input).value = ""
        if update_endpoint:
            self._endpoint_url_managed = True
        for candidate in self.service_catalog:
            button = self.query_one(
                f"#provider-settings-preset-{candidate.ui_key or candidate.service_id}",
                Button,
            )
            button.variant = (
                "primary"
                if (candidate.ui_key or candidate.service_id) == self._active_preset
                else "default"
            )
        self.query_one("#provider-settings-model", Input).placeholder = self._service_text(
            service.model_placeholder_key,
            service.display_name,
        )
        if update_endpoint:
            self._set_endpoint_url(
                service.endpoint_for(
                    protocol=self._active_protocol,
                    variant_id=self._active_endpoint_variant,
                )
            )
        self._refresh_provider_controls(service)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "provider-settings-base-url" and not self._updating_endpoint:
            self._endpoint_url_managed = False
            return
        if event.input.id == "provider-settings-model":
            service = self._active_service()
            if service is not None:
                if self._protocol_auto:
                    previous_protocol = self._active_protocol
                    self._active_protocol = self._recommended_protocol(service)
                    if previous_protocol != self._active_protocol and self._endpoint_url_managed:
                        self._set_endpoint_url(
                            service.endpoint_for(
                                protocol=self._active_protocol,
                                variant_id=self._active_endpoint_variant,
                            )
                        )
                        self._clear_model_catalog()
                    self._refresh_provider_controls(service)
                else:
                    self._refresh_protocol_controls(service)

    def _select_proxy_mode(self, proxy_mode: str | None) -> None:
        if proxy_mode not in {None, "environment", "direct", "explicit"}:
            return
        self._clear_model_catalog()
        self._active_proxy_mode = proxy_mode
        for candidate in (None, "environment", "direct", "explicit"):
            name = "inherit" if candidate is None else candidate
            button = self.query_one(f"#provider-settings-proxy-{name}", Button)
            button.variant = "primary" if candidate == proxy_mode else "default"
        proxy_env = self.query_one("#provider-settings-proxy-env", Input)
        proxy_env.disabled = proxy_mode != "explicit"
        self.query_one("#provider-settings-proxy-hint", Static).update(
            ui_text(
                self.language,
                (
                    "provider_settings.proxy.hint.inherit"
                    if proxy_mode is None
                    else f"provider_settings.proxy.hint.{proxy_mode}"
                ),
                policy=self._global_proxy_policy_label(),
            )
        )
        self._reset_delete_confirmation()

    def _global_proxy_policy_label(self) -> str:
        return ui_text(
            self.language,
            f"network_settings.policy.{self.provider_settings.proxy_defaults.mode}",
        )

    def _select_background_wake_policy(
        self,
        policy: BackgroundTaskWakePolicy | None,
    ) -> None:
        self._active_background_wake_policy = policy
        for candidate in (None, *BackgroundTaskWakePolicy):
            name = "inherit" if candidate is None else candidate.value
            self.query_one(f"#provider-settings-wake-{name}", Button).variant = (
                "primary" if candidate is policy else "default"
            )
        self.query_one("#provider-settings-background-wake-hint", Static).update(
            ui_text(
                self.language,
                "provider_settings.background_wake.hint",
            )
        )


__all__ = ["ProviderInteractionMixin"]
