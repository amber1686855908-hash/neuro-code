"""Typed host surface shared by the Provider settings screen mixins.

Provider 设置屏幕 mixin 共享的类型宿主表面.

The live Textual screen remains the sole owner of mutable state.  This module
only describes the composition surface needed by the cohesive mixins; it does
not create a second state holder or a service container.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from neuro_code.application.ports.provider_catalog import ProviderCatalog
    from neuro_code.application.ports.provider_services import (
        ProviderServiceCatalog,
    )
    from neuro_code.application.ports.provider_settings import (
        ManagedProviderSettings,
        ProviderSettingsStore,
    )
    from neuro_code.shared.ui_language import UiLanguage


class ProviderSettingsScreenMixin:
    """Static composition contract for Provider settings screen mixins."""

    if TYPE_CHECKING:
        language: UiLanguage
        provider_settings: ManagedProviderSettings
        provider_settings_store: ProviderSettingsStore
        provider_catalog: ProviderCatalog | None
        service_catalog: ProviderServiceCatalog
        socks_supported: bool
        first_run: bool
        initial_profile: str | None
        initial_error: str | None
        _editing_profile: str | None
        _active_preset: str
        _active_protocol: str
        _protocol_auto: bool
        _active_endpoint_variant: str | None
        _endpoint_url_managed: bool
        _updating_endpoint: bool
        _active_proxy_mode: str | None
        _active_background_wake_policy: Any
        _delete_confirmation_for: str | None
        _catalog_model_ids: dict[str, str]
        _profile_ids: dict[str, str]
        _RECOMMENDED_PROTOCOL: ClassVar[str]
        _PROTOCOL_SELECTION_ORDER: ClassVar[tuple[str, ...]]

        def __getattr__(self, name: str) -> Any: ...


__all__ = ["ProviderSettingsScreenMixin"]
