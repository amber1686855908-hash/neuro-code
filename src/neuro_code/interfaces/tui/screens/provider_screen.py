"""Provider setup and profile editor screens.

Provider 配置与配置档编辑屏幕.
"""

from __future__ import annotations

from typing import Any, ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static

from neuro_code.application.ports.provider_catalog import (
    ProviderCatalog,
)
from neuro_code.application.ports.provider_services import (
    DEFAULT_PROVIDER_SERVICE_CATALOG,
    ProviderServiceCatalog,
    ProviderServiceDescriptor,
)
from neuro_code.application.ports.provider_settings import (
    ManagedProviderProfile,
    ManagedProviderSettings,
    ProviderSettingsStore,
)
from neuro_code.domain.background_tasks.models import BackgroundTaskWakePolicy
from neuro_code.interfaces.tui.screens.provider_catalog import ProviderCatalogMixin
from neuro_code.interfaces.tui.screens.provider_draft import ProviderDraftMixin
from neuro_code.interfaces.tui.screens.provider_interaction import ProviderInteractionMixin
from neuro_code.interfaces.tui.screens.provider_persistence import ProviderPersistenceMixin
from neuro_code.interfaces.tui.state import (
    _ERROR_MARK,
    ProviderSettingsSubmission,
)
from neuro_code.interfaces.tui.text import ui_text
from neuro_code.interfaces.tui.theme import ERROR_TEXT_STYLE
from neuro_code.shared.ui_language import UiLanguage


class ProviderSettingsScreen(
    ProviderInteractionMixin,
    ProviderDraftMixin,
    ProviderCatalogMixin,
    ProviderPersistenceMixin,
    ModalScreen[ProviderSettingsSubmission | None],
):
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

    def _show_provider_error(self, message: str) -> None:
        self.query_one("#provider-settings-error", Static).update(
            Text(f"{_ERROR_MARK} {message}", style=ERROR_TEXT_STYLE)
        )

    def action_cancel(self) -> None:
        self.dismiss(None)


__all__ = ["ProviderSettingsScreen"]
