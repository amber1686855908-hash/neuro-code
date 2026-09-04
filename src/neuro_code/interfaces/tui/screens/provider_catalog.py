"""Model catalog discovery and status presentation for Provider settings.

Provider 设置的模型目录发现与状态展示.

The injected application port remains the only discovery boundary.  This
module owns the asynchronous probe lifecycle and its screen-local rendering,
including stale-result and fallback handling.
"""

from __future__ import annotations

from rich.text import Text
from textual.containers import VerticalScroll
from textual.widgets import Button, Input, Static

from neuro_code.application.ports.provider_catalog import (
    ProviderCatalogError,
    ProviderCatalogResult,
    ProviderConnectionSpec,
)
from neuro_code.application.ports.provider_services import ModelCatalogStrategy
from neuro_code.interfaces.tui.screens.provider_context import ProviderSettingsScreenMixin
from neuro_code.interfaces.tui.state import _ERROR_MARK, _SUCCESS_MARK, _WARNING_MARK
from neuro_code.interfaces.tui.text import ui_text
from neuro_code.interfaces.tui.theme import (
    CONNECTION_STATUS_STYLES,
    TEXT_SECONDARY,
)
from neuro_code.shared.redaction import redact_sensitive_text


class ProviderCatalogMixin(ProviderSettingsScreenMixin):
    """Own model discovery, catalog rendering, and discovery status mapping."""

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


__all__ = ["ProviderCatalogMixin"]
