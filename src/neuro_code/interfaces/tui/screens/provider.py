"""Provider setup and profile editor screens.

Provider 配置与配置档编辑屏幕.
"""

from __future__ import annotations

import os
from typing import Any, ClassVar

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static

from neuro_code.application.ports.configuration import resolve_http_client_policy
from neuro_code.application.ports.http import HttpClientPolicy
from neuro_code.application.ports.model import ModelCapabilitySet
from neuro_code.application.ports.provider_catalog import (
    ProviderCatalog,
    ProviderCatalogError,
    ProviderCatalogResult,
    ProviderConnectionSpec,
)
from neuro_code.application.ports.provider_services import (
    DEFAULT_PROVIDER_SERVICE_CATALOG,
    ModelCatalogStrategy,
    ProtocolSupportStatus,
    ProviderServiceCatalog,
    ProviderServiceDescriptor,
)
from neuro_code.application.ports.provider_settings import (
    ManagedProviderProfile,
    ManagedProviderSettings,
    ManagedProxyPolicy,
    ProviderSettingsStore,
)
from neuro_code.domain.background_tasks.models import BackgroundTaskWakePolicy
from neuro_code.interfaces.tui.state import (
    _ERROR_MARK,
    _SUCCESS_MARK,
    _WARNING_MARK,
    ProviderSettingsSubmission,
)
from neuro_code.interfaces.tui.text import ui_text
from neuro_code.interfaces.tui.theme import (
    CONNECTION_STATUS_STYLES,
    ERROR_TEXT_STYLE,
    TEXT_SECONDARY,
    TEXTUAL_THEME,
)
from neuro_code.shared.redaction import redact_sensitive_text
from neuro_code.shared.ui_language import UiLanguage


class ProviderSettingsScreen(ModalScreen[ProviderSettingsSubmission | None]):
    """Create and edit user-owned provider profiles on a focused detail screen.

    在聚焦的详情界面创建和编辑用户拥有的 Provider 配置."""

    CSS = """
    ProviderSettingsScreen {
        align: center middle;
        background: $background 85%;
    }

    #provider-settings-dialog {
        width: 92%;
        max-width: 116;
        height: 95%;
        max-height: 95%;
        padding: $space-2 $space-3;
        border: solid $border;
        background: $surface;
    }

    #provider-settings-content {
        height: 1fr;
    }

    #provider-settings-title {
        text-style: bold;
        color: $text-primary;
        margin-bottom: 1;
    }

    #provider-settings-description,
    #provider-settings-protocol-hint,
    #provider-settings-proxy-title,
    #provider-settings-proxy-hint,
    #provider-settings-context-hint,
    #provider-settings-connection-status,
    #provider-settings-error,
    #provider-settings-empty {
        color: $text-muted;
        margin-bottom: 1;
    }

    #provider-settings-protocol-hint {
        color: $text-secondary;
    }

    #provider-settings-proxy-title {
        color: $text;
        text-style: bold;
        margin-top: 1;
    }

    #provider-settings-proxy-hint {
        color: $text-secondary;
    }

    #provider-settings-connection-status {
        margin-top: 1;
        margin-bottom: 1;
    }

    #provider-settings-error {
        padding-left: 1;
        border-left: tall $border-focus;
        color: $text-primary;
        text-style: bold;
    }

    #provider-settings-profiles {
        height: auto;
        max-height: 8;
        margin-bottom: 1;
    }

    #provider-settings-models {
        display: none;
        height: auto;
        max-height: 10;
        margin-bottom: 1;
    }

    #provider-settings-models Button {
        width: 100%;
        margin-bottom: 1;
    }

    #provider-settings-profiles Button {
        width: 100%;
        margin-bottom: 1;
    }

    #provider-settings-presets,
    #provider-settings-presets-row-one,
    #provider-settings-presets-row-two,
    #provider-settings-endpoints,
    #provider-settings-protocols,
    #provider-settings-proxy-modes,
    #provider-settings-wake-modes,
    #provider-settings-form,
    #provider-settings-actions {
        height: auto;
    }

    #provider-settings-presets {
        margin-bottom: 1;
    }

    #provider-settings-presets Button {
        width: 1fr;
        margin-right: 1;
    }

    #provider-settings-endpoints Button,
    #provider-settings-protocols Button {
        width: 1fr;
        margin-right: 1;
    }

    #provider-settings-endpoint-title,
    #provider-settings-protocol-title {
        color: $text-primary;
        margin-top: 1;
    }

    #provider-settings-presets-row-one {
        margin-bottom: 1;
    }

    #provider-settings-form Input {
        margin-bottom: 0;
    }

    #provider-settings-form Label {
        color: $text-primary;
        margin-top: 1;
    }

    #provider-settings-proxy-modes Button {
        width: 1fr;
        margin-right: 1;
    }

    #provider-settings-wake-modes Button {
        width: 1fr;
        margin-right: 1;
    }

    #provider-settings-form {
        margin-bottom: 1;
    }

    #provider-settings-actions {
        align-horizontal: right;
    }

    #provider-settings-actions Button {
        margin-left: 1;
    }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Back", show=False),
        Binding("ctrl+c", "cancel", "Back", show=False),
    ]
    _RECOMMENDED_PROTOCOL = "recommended"
    _PROTOCOL_SELECTION_ORDER = (
        "openai-chat",
        "openai-responses",
        "anthropic-messages",
        "gemini-generate-content",
        "gemini-interactions",
    )

    def __init__(
        self,
        *,
        language: UiLanguage,
        provider_settings: ManagedProviderSettings,
        provider_settings_store: ProviderSettingsStore,
        provider_catalog: ProviderCatalog | None = None,
        service_catalog: ProviderServiceCatalog | None = None,
        socks_supported: bool = False,
        first_run: bool = False,
        initial_profile: str | None = None,
        initial_error: str | None = None,
    ) -> None:
        super().__init__()
        self.language = language
        self.provider_settings = provider_settings
        self.provider_settings_store = provider_settings_store
        self.provider_catalog = provider_catalog
        self.service_catalog = service_catalog or DEFAULT_PROVIDER_SERVICE_CATALOG
        self.socks_supported = socks_supported
        self.first_run = first_run
        self.initial_profile = initial_profile
        self.initial_error = initial_error
        self._editing_profile: str | None = None
        self._active_preset = self._default_service_key()
        self._active_protocol = self._default_service().default_protocol
        self._protocol_auto = False
        default_endpoint_variant = self._default_service().default_endpoint_variant
        self._active_endpoint_variant: str | None = (
            default_endpoint_variant.variant_id if default_endpoint_variant is not None else None
        )
        self._endpoint_url_managed = True
        self._updating_endpoint = False
        self._active_proxy_mode: str | None = None
        self._active_background_wake_policy: BackgroundTaskWakePolicy | None = None
        self._delete_confirmation_for: str | None = None
        self._catalog_model_ids: dict[str, str] = {}
        self._profile_ids = {
            f"provider-settings-profile-{index}": profile.name
            for index, profile in enumerate(provider_settings.profiles)
        }

    def compose(self) -> ComposeResult:
        default_service = self._default_service()
        profile_widgets: list[Any] = [
            Button(
                self._provider_label(profile),
                id=f"provider-settings-profile-{index}",
                variant=(
                    "primary"
                    if profile.name == self.provider_settings.default_provider
                    else "default"
                ),
            )
            for index, profile in enumerate(self.provider_settings.profiles)
        ]
        if not profile_widgets:
            profile_widgets.append(
                Static(
                    ui_text(self.language, "provider_settings.empty"), id="provider-settings-empty"
                )
            )
        preset_buttons = [
            Button(
                self._service_label(service),
                id=f"provider-settings-preset-{service.ui_key or service.service_id}",
                variant=(
                    "primary"
                    if (service.ui_key or service.service_id) == self._active_preset
                    else "default"
                ),
            )
            for service in self.service_catalog
        ]
        preset_rows = [
            Horizontal(
                *preset_buttons[index : index + 3],
                id=f"provider-settings-presets-row-{index // 3}",
            )
            for index in range(0, len(preset_buttons), 3)
        ]
        endpoint_buttons = [
            Button(
                variant.display_name,
                id=f"provider-settings-endpoint-{variant.variant_id}",
                variant="primary"
                if (service.ui_key or service.service_id) == self._active_preset
                and variant.variant_id == self._active_endpoint_variant
                else "default",
            )
            for service in self.service_catalog
            for variant in service.endpoint_variants
        ]
        protocol_order = (self._RECOMMENDED_PROTOCOL, *self._PROTOCOL_SELECTION_ORDER)
        protocol_buttons = [
            Button(
                self._protocol_label(protocol),
                id=f"provider-settings-protocol-{protocol}",
                variant=(
                    "primary"
                    if (
                        self._protocol_auto
                        if protocol == self._RECOMMENDED_PROTOCOL
                        else not self._protocol_auto and protocol == self._active_protocol
                    )
                    else "default"
                ),
            )
            for protocol in protocol_order
        ]
        actions: list[Any] = []
        if not self.first_run:
            actions.extend(
                (
                    Button(ui_text(self.language, "settings.back"), id="provider-settings-back"),
                    Button(
                        ui_text(self.language, "provider_settings.delete"),
                        id="provider-settings-delete",
                        disabled=True,
                    ),
                )
            )
        actions.extend(
            (
                Button(
                    ui_text(self.language, "provider_settings.new"),
                    id="provider-settings-new",
                ),
                Button(
                    ui_text(self.language, "provider_settings.connection.test"),
                    id="provider-settings-test",
                    disabled=self.provider_catalog is None,
                ),
                Button(
                    ui_text(self.language, "provider_settings.save_use"),
                    id="provider-settings-save",
                    variant="success",
                ),
            )
        )
        yield Vertical(
            VerticalScroll(
                Label(
                    ui_text(
                        self.language,
                        "provider_settings.first_run_title"
                        if self.first_run
                        else "provider_settings.title",
                    ),
                    id="provider-settings-title",
                ),
                Static(
                    ui_text(self.language, "provider_settings.description"),
                    id="provider-settings-description",
                ),
                VerticalScroll(*profile_widgets, id="provider-settings-profiles"),
                Vertical(
                    *preset_rows,
                    id="provider-settings-presets",
                ),
                Static(
                    ui_text(self.language, "provider_settings.endpoint.title"),
                    id="provider-settings-endpoint-title",
                ),
                Horizontal(*endpoint_buttons, id="provider-settings-endpoints"),
                Static(
                    ui_text(self.language, "provider_settings.protocol.title"),
                    id="provider-settings-protocol-title",
                ),
                Horizontal(*protocol_buttons, id="provider-settings-protocols"),
                Static(
                    self._service_text(
                        default_service.protocol_hint_for(self._active_protocol),
                        f"{default_service.display_name} · {default_service.default_protocol}",
                    ),
                    id="provider-settings-protocol-hint",
                ),
                Vertical(
                    Label(ui_text(self.language, "provider_settings.field.name")),
                    Input(
                        placeholder=ui_text(self.language, "provider_settings.name"),
                        id="provider-settings-name",
                    ),
                    Label(ui_text(self.language, "provider_settings.field.model")),
                    Input(
                        placeholder=self._service_text(
                            default_service.model_placeholder_key,
                            default_service.display_name,
                        ),
                        id="provider-settings-model",
                    ),
                    Label(ui_text(self.language, "provider_settings.field.base_url")),
                    Input(
                        value=default_service.default_base_url,
                        placeholder=ui_text(self.language, "provider_settings.base_url"),
                        id="provider-settings-base-url",
                    ),
                    Label(ui_text(self.language, "provider_settings.field.api_key")),
                    Input(
                        placeholder=ui_text(self.language, "provider_settings.api_key"),
                        password=True,
                        id="provider-settings-api-key",
                    ),
                    Label(ui_text(self.language, "provider_settings.field.context_window")),
                    Input(
                        placeholder=ui_text(self.language, "provider_settings.context_window"),
                        id="provider-settings-context-window",
                    ),
                    Static(
                        ui_text(self.language, "provider_settings.context_window_hint"),
                        id="provider-settings-context-hint",
                    ),
                    Static(
                        ui_text(self.language, "provider_settings.proxy.title"),
                        id="provider-settings-proxy-title",
                    ),
                    Horizontal(
                        Button(
                            ui_text(self.language, "provider_settings.proxy.inherit"),
                            id="provider-settings-proxy-inherit",
                            variant="primary",
                        ),
                        Button(
                            ui_text(self.language, "provider_settings.proxy.environment"),
                            id="provider-settings-proxy-environment",
                        ),
                        Button(
                            ui_text(self.language, "provider_settings.proxy.direct"),
                            id="provider-settings-proxy-direct",
                        ),
                        Button(
                            ui_text(self.language, "provider_settings.proxy.explicit"),
                            id="provider-settings-proxy-explicit",
                        ),
                        id="provider-settings-proxy-modes",
                    ),
                    Input(
                        placeholder=ui_text(
                            self.language,
                            "provider_settings.proxy.environment_variable",
                        ),
                        id="provider-settings-proxy-env",
                        disabled=True,
                    ),
                    Static(
                        ui_text(
                            self.language,
                            "provider_settings.proxy.hint.environment",
                        ),
                        id="provider-settings-proxy-hint",
                    ),
                    Static(
                        ui_text(
                            self.language,
                            "provider_settings.background_wake.title",
                        ),
                        id="provider-settings-background-wake-title",
                    ),
                    Horizontal(
                        Button(
                            ui_text(
                                self.language,
                                "provider_settings.background_wake.inherit",
                            ),
                            id="provider-settings-wake-inherit",
                            variant="primary",
                        ),
                        Button(
                            ui_text(
                                self.language,
                                "provider_settings.background_wake.disabled",
                            ),
                            id="provider-settings-wake-disabled",
                        ),
                        Button(
                            ui_text(
                                self.language,
                                "provider_settings.background_wake.enabled",
                            ),
                            id="provider-settings-wake-enabled",
                        ),
                        id="provider-settings-wake-modes",
                    ),
                    Static(
                        ui_text(
                            self.language,
                            "provider_settings.background_wake.hint",
                        ),
                        id="provider-settings-background-wake-hint",
                    ),
                    Static("", id="provider-settings-connection-status"),
                    VerticalScroll(id="provider-settings-models"),
                    id="provider-settings-form",
                ),
                Static("", id="provider-settings-error"),
                id="provider-settings-content",
            ),
            Horizontal(*actions, id="provider-settings-actions"),
            id="provider-settings-dialog",
            classes="modal-dialog modal-l",
        )

    def _provider_label(self, profile: ManagedProviderProfile) -> str:
        suffix = (
            f" · {ui_text(self.language, 'marker.default')}"
            if profile.name == self.provider_settings.default_provider
            else ""
        )
        return f"{profile.name} · {profile.model}{suffix}"

    def _service_label(self, service: ProviderServiceDescriptor) -> str:
        if service.label_key is not None:
            try:
                return ui_text(self.language, service.label_key)
            except KeyError:
                pass
        return service.display_name

    def _protocol_label(self, protocol: str) -> str:
        key = {
            self._RECOMMENDED_PROTOCOL: "provider_settings.protocol.option.recommended",
            "openai-chat": "provider_settings.protocol.option.chat",
            "openai-responses": "provider_settings.protocol.option.responses",
            "anthropic-messages": "provider_settings.protocol.option.anthropic",
            "gemini-generate-content": "provider_settings.protocol.option.gemini",
            "gemini-interactions": "provider_settings.protocol.option.gemini_interactions",
        }.get(protocol)
        if key is None:
            return protocol
        try:
            return ui_text(self.language, key)
        except KeyError:
            return protocol

    def _service_text(self, key: str | None, fallback: str, **values: object) -> str:
        if key is not None:
            try:
                return ui_text(self.language, key, **values)
            except KeyError:
                pass
        return fallback

    def _service(self, identifier: str) -> ProviderServiceDescriptor | None:
        return self.service_catalog.get(identifier)

    def _active_service(self) -> ProviderServiceDescriptor | None:
        return self._service(self._active_preset)

    def _model_protocol_status(
        self,
        service: ProviderServiceDescriptor,
        protocol: str,
    ) -> ProtocolSupportStatus:
        model = self.query_one("#provider-settings-model", Input).value.strip()
        if not model:
            return (
                ProtocolSupportStatus.SUPPORTED
                if protocol in service.supported_protocols
                else ProtocolSupportStatus.UNSUPPORTED
            )
        return service.protocol_support_for(model=model, protocol=protocol)

    def _recommended_protocol(self, service: ProviderServiceDescriptor) -> str:
        """Choose a concrete protocol without persisting an auto sentinel.

        Chat is the portable fallback.  A documented Responses or Anthropic
        route wins over an unknown Chat route, while unknown models retain the
        service default instead of silently changing wire protocols.
        自动选择只存在于设置界面,保存时始终落成具体协议.
        """

        model = self.query_one("#provider-settings-model", Input).value.strip()
        available = tuple(
            protocol
            for protocol in self._PROTOCOL_SELECTION_ORDER
            if protocol in service.supported_protocols
        )
        if not available:
            return service.default_protocol
        if not model:
            return (
                service.default_protocol if service.default_protocol in available else available[0]
            )
        for protocol in available:
            if self._model_protocol_status(service, protocol) is ProtocolSupportStatus.SUPPORTED:
                return protocol
        return service.default_protocol if service.default_protocol in available else available[0]

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

    def _draft_proxy_policy(self) -> ManagedProxyPolicy:
        if self._active_proxy_mode is None:
            return self.provider_settings.proxy_defaults
        proxy_url_env = (
            self.query_one("#provider-settings-proxy-env", Input).value.strip() or None
            if self._active_proxy_mode == "explicit"
            else None
        )
        return ManagedProxyPolicy(self._active_proxy_mode, proxy_url_env)

    def _context_window_tokens(self) -> int | None:
        value = self.query_one("#provider-settings-context-window", Input).value.strip()
        if not value:
            return None
        try:
            context_window_tokens = int(value)
        except ValueError as error:
            raise ValueError(
                ui_text(self.language, "provider_settings.context_window_invalid")
            ) from error
        if context_window_tokens <= 0:
            raise ValueError(ui_text(self.language, "provider_settings.context_window_invalid"))
        return context_window_tokens

    def _connection_spec(self) -> tuple[ProviderConnectionSpec, HttpClientPolicy]:
        service = self._service(self._active_preset)
        if service is None:
            raise ValueError("provider service selection is unavailable")
        base_url = self.query_one("#provider-settings-base-url", Input).value.strip()
        name = self.query_one("#provider-settings-name", Input).value.strip()
        existing = self.provider_settings.profile(name)
        entered_api_key = self.query_one("#provider-settings-api-key", Input).value.strip()
        api_key = entered_api_key or (existing.api_key if existing is not None else None)
        if api_key is None:
            raise ValueError(ui_text(self.language, "provider_settings.api_key_required"))
        proxy_policy = self._draft_proxy_policy()
        policy = resolve_http_client_policy(
            proxy_mode=proxy_policy.mode,
            proxy_url_env=proxy_policy.proxy_url_env,
            environ=os.environ,
            socks_supported=self.socks_supported,
        )
        return (
            ProviderConnectionSpec(
                protocol=self._active_protocol,
                dialect=service.dialect_for(self._active_protocol),
                base_url=base_url,
                api_key=api_key,
                service_id=service.service_id,
                catalog_strategy=service.catalog_strategy_for(self._active_protocol),
            ),
            policy,
        )

    async def _test_connection(self) -> None:
        if self.provider_catalog is None:
            return
        service = self._service(self._active_preset)
        if service is None:
            self._show_provider_error("provider service selection is unavailable")
            return
        button = self.query_one("#provider-settings-test", Button)
        button.disabled = True
        button.label = ui_text(self.language, "provider_settings.connection.testing")
        self._clear_model_catalog()
        self._show_provider_error("")
        self._show_connection_status(
            ui_text(self.language, "provider_settings.connection.testing"),
            kind="normal",
        )
        signature: tuple[str, ...] | None = None
        spec: ProviderConnectionSpec | None = None
        try:
            catalog_strategy = service.catalog_strategy_for(self._active_protocol)
            if catalog_strategy is ModelCatalogStrategy.STATIC:
                await self._show_model_catalog(ProviderCatalogResult(service.static_models))
                self._show_connection_status(
                    ui_text(self.language, "provider_settings.connection.static"),
                    kind="warning",
                )
                return
            if catalog_strategy is ModelCatalogStrategy.MANUAL_ONLY:
                self._show_connection_status(
                    ui_text(self.language, "provider_settings.connection.manual_only"),
                    kind="warning",
                )
                return
            spec, policy = self._connection_spec()
            signature = self._connection_signature()
            result = await self.provider_catalog.discover_models(spec, http_policy=policy)
            if signature != self._connection_signature():
                self._show_connection_status(
                    ui_text(self.language, "provider_settings.connection.stale"),
                    kind="warning",
                )
                return
            await self._show_model_catalog(result)
        except Exception as error:
            if signature is not None and signature != self._connection_signature():
                self._show_connection_status(
                    ui_text(self.language, "provider_settings.connection.stale"),
                    kind="warning",
                )
            elif (
                isinstance(error, ProviderCatalogError)
                and error.kind in {"endpoint", "network", "proxy", "server", "timeout"}
                and service.static_models
            ):
                await self._show_model_catalog(ProviderCatalogResult(service.static_models))
                self._show_connection_status(
                    ui_text(self.language, "provider_settings.connection.fallback"),
                    kind="warning",
                )
            else:
                self._show_connection_status(
                    self._connection_error_message(
                        error,
                        api_key=spec.api_key if spec is not None else None,
                    ),
                    kind="error",
                )
        finally:
            button.disabled = False
            button.label = ui_text(self.language, "provider_settings.connection.test")

    def _connection_signature(self) -> tuple[str, ...]:
        return (
            self._active_preset,
            self._active_protocol,
            self._active_endpoint_variant or "default",
            self._active_proxy_mode or "inherit",
            self.provider_settings.proxy_defaults.mode,
            self.provider_settings.proxy_defaults.proxy_url_env or "",
            self.query_one("#provider-settings-name", Input).value,
            self.query_one("#provider-settings-base-url", Input).value,
            self.query_one("#provider-settings-api-key", Input).value,
            self.query_one("#provider-settings-proxy-env", Input).value,
        )

    async def _show_model_catalog(self, result: ProviderCatalogResult) -> None:
        container = self.query_one("#provider-settings-models", VerticalScroll)
        await container.remove_children()
        self._catalog_model_ids = {
            f"provider-settings-catalog-model-{index}": model
            for index, model in enumerate(result.models)
        }
        if result.models:
            await container.mount(
                *(
                    Button(Text(model), id=button_id)
                    for button_id, model in self._catalog_model_ids.items()
                )
            )
            container.display = True
        else:
            container.display = False
        selected_model = self.query_one("#provider-settings-model", Input).value.strip()
        if not result.models:
            message = ui_text(self.language, "provider_settings.connection.success_empty")
            kind = "success"
        elif selected_model and selected_model in result.models:
            message = ui_text(
                self.language,
                "provider_settings.connection.success_selected",
                count=len(result.models),
                model=selected_model,
            )
            kind = "success"
        elif selected_model:
            message = ui_text(
                self.language,
                "provider_settings.connection.success_missing",
                count=len(result.models),
                model=selected_model,
            )
            kind = "warning"
        else:
            message = ui_text(
                self.language,
                "provider_settings.connection.success",
                count=len(result.models),
            )
            kind = "success"
        if result.truncated:
            message += ui_text(self.language, "provider_settings.connection.truncated")
        self._show_connection_status(message, kind=kind)

    def _connection_error_message(self, error: Exception, *, api_key: str | None = None) -> str:
        if isinstance(error, ProviderCatalogError):
            key = {
                "authentication": "authentication",
                "endpoint": "endpoint",
                "timeout": "timeout",
                "rate_limit": "rate_limit",
                "server": "server",
                "http": "http",
                "proxy": "proxy",
                "network": "network",
                "response_too_large": "response_too_large",
                "invalid_response": "invalid_response",
            }.get(error.kind, "unknown")
            return ui_text(
                self.language,
                f"provider_settings.connection.error.{key}",
                status=error.status_code if error.status_code is not None else "?",
                detail=error.detail or ui_text(self.language, "value.unknown"),
            )
        entered_api_key = self.query_one("#provider-settings-api-key", Input).value.strip()
        return redact_sensitive_text(str(error), explicit_values=(entered_api_key, api_key or ""))

    def _clear_model_catalog(self) -> None:
        self._catalog_model_ids = {}
        if self.is_mounted:
            self.query_one("#provider-settings-models", VerticalScroll).display = False
            self.query_one("#provider-settings-connection-status", Static).update("")

    def _show_connection_status(self, message: str, *, kind: str) -> None:
        color = CONNECTION_STATUS_STYLES.get(kind, TEXT_SECONDARY)
        marker = {
            "success": _SUCCESS_MARK,
            "warning": _WARNING_MARK,
            "error": _ERROR_MARK,
        }.get(kind, "…")
        self.query_one("#provider-settings-connection-status", Static).update(
            Text(f"{marker} {message}", style=color)
        )

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

    def _show_provider_error(self, message: str) -> None:
        self.query_one("#provider-settings-error", Static).update(
            Text(f"{_ERROR_MARK} {message}", style=ERROR_TEXT_STYLE)
        )

    def action_cancel(self) -> None:
        self.dismiss(None)


class ProviderSetupApp(App[bool]):
    """Focused provider setup used for first run and recoverable startup errors.

    用于首次运行和可恢复启动错误的聚焦 Provider 配置界面."""

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


__all__ = ["ProviderSettingsScreen", "ProviderSetupApp"]
