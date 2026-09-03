"""Draft extraction and validation for the Provider settings screen.

Provider 设置屏幕的草稿提取与校验.

This module owns the translation from visible form state to ephemeral
application-port values.  It deliberately does not save settings or perform
network discovery.
"""

from __future__ import annotations

import os

from textual.widgets import Input

from neuro_code.application.ports.configuration import resolve_http_client_policy
from neuro_code.application.ports.http import HttpClientPolicy
from neuro_code.application.ports.provider_catalog import ProviderConnectionSpec
from neuro_code.application.ports.provider_services import (
    ProtocolSupportStatus,
    ProviderServiceDescriptor,
)
from neuro_code.application.ports.provider_settings import ManagedProxyPolicy
from neuro_code.interfaces.tui.screens.provider_context import ProviderSettingsScreenMixin
from neuro_code.interfaces.tui.text import ui_text


class ProviderDraftMixin(ProviderSettingsScreenMixin):
    """Build and validate ephemeral Provider settings drafts."""

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


__all__ = ["ProviderDraftMixin"]
