"""Compatibility facade for canonical application configuration.

提供应用配置的兼容门面,并转发到规范实现."""

from pathlib import Path as _Path

from neuro_code.configuration.app import (
    SUPPORTED_AUTH,
    SUPPORTED_DIALECTS,
    SUPPORTED_NATIVE_CONTEXT,
    SUPPORTED_PROTOCOLS,
    SUPPORTED_PROXY_MODES,
    AppConfig,
    ProviderProfile,
    load_config,
    override_provider,
    override_sandbox,
    pin_resumed_sandbox,
    resolve_http_client_policy,
)

# Keep the historical module attribute available for tests and integrations
# that patch ``neuro_code.config.Path.home`` while the implementation lives in
# the canonical configuration module.
Path = _Path

__all__ = [
    "SUPPORTED_AUTH",
    "SUPPORTED_DIALECTS",
    "SUPPORTED_NATIVE_CONTEXT",
    "SUPPORTED_PROTOCOLS",
    "SUPPORTED_PROXY_MODES",
    "AppConfig",
    "ProviderProfile",
    "load_config",
    "override_provider",
    "override_sandbox",
    "pin_resumed_sandbox",
    "resolve_http_client_policy",
]
