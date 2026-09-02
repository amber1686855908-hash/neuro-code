"""Application configuration values and policy.

Configuration value objects are an application-facing boundary contract. File,
environment, and managed-settings loading belongs to ``bootstrap.configuration``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

from neuro_code.application.ports.http import HttpClientPolicy
from neuro_code.application.ports.lsp import LanguageServerProfile
from neuro_code.application.ports.model import CapabilityResolution, ModelCapabilitySet
from neuro_code.application.ports.provider_services import (
    DEFAULT_PROVIDER_SERVICE_CATALOG,
    SUPPORTED_DIALECTS,
    SUPPORTED_PROTOCOLS,
    ProtocolSupportStatus,
)
from neuro_code.application.ports.routing import ModelRoute, RuntimeRole
from neuro_code.application.ports.web_fetch import WebFetchMode
from neuro_code.application.ports.web_search import WebSearchMode
from neuro_code.domain.sandbox.models import SandboxProfile
from neuro_code.shared.errors import ConfigurationError

SUPPORTED_AUTH = frozenset({"env", "stored", "proxy-managed", "unsupported-inline"})
SUPPORTED_NATIVE_CONTEXT = frozenset({"disabled", "profile"})
SUPPORTED_PROXY_MODES = frozenset({"environment", "direct", "explicit"})

__all__ = [
    "SUPPORTED_AUTH",
    "SUPPORTED_DIALECTS",
    "SUPPORTED_NATIVE_CONTEXT",
    "SUPPORTED_PROTOCOLS",
    "SUPPORTED_PROXY_MODES",
    "AppConfig",
    "ProviderProfile",
    "override_provider",
    "override_sandbox",
    "pin_resumed_sandbox",
    "resolve_http_client_policy",
]
_XAI_BUILTIN_TOOLS = frozenset({"web_search", "x_search", "code_interpreter"})
_OPENAI_RESPONSES_BUILTIN_TOOLS = frozenset({"web_search"})
_ANTHROPIC_BUILTIN_TOOLS = frozenset({"web_search", "web_fetch"})
_GEMINI_INTERACTIONS_BUILTIN_TOOLS = frozenset({"google_search", "url_context"})
_PROXY_ENVIRONMENT_VARIABLES = frozenset({"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"})
_HTTP_PROXY_SCHEMES = frozenset({"http", "https"})
_SOCKS_PROXY_SCHEMES = frozenset({"socks5", "socks5h"})


def _canonical_url(value: str) -> str:
    stripped = value.rstrip("/")
    try:
        parts = urlsplit(stripped)
        hostname = parts.hostname
        if not parts.scheme or hostname is None:
            return stripped
        port = parts.port
    except ValueError:
        return stripped
    host = hostname.casefold()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if port is not None:
        host = f"{host}:{port}"
    return urlunsplit((parts.scheme.casefold(), host, parts.path.rstrip("/"), "", ""))


def _is_loopback_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        return (
            parsed.scheme == "http"
            and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
            and parsed.username is None
            and parsed.password is None
            and parsed.query == ""
            and parsed.fragment == ""
        )
    except ValueError:
        return False


def _validate_base_url(value: str) -> None:
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError as error:
        raise ConfigurationError(f"provider base_url is invalid: {error}") from error
    if parsed.scheme not in {"http", "https"} or hostname is None:
        raise ConfigurationError("provider base_url must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ConfigurationError("provider base_url must not contain user information")
    if parsed.query or parsed.fragment:
        raise ConfigurationError("provider base_url must not contain a query or fragment")


def _proxy_environment_values(environ: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    return tuple(
        (name, value.strip())
        for name, value in environ.items()
        if name.upper() in _PROXY_ENVIRONMENT_VARIABLES and value.strip()
    )


def _proxy_redaction_values(value: str) -> tuple[str, ...]:
    redactions = [value]
    try:
        parsed = urlsplit(value)
        for credential in (parsed.username, parsed.password):
            if credential:
                redactions.extend((credential, unquote(credential)))
    except ValueError:
        pass
    return tuple(dict.fromkeys(redactions))


def _validate_proxy_url(value: str, *, source: str, socks_supported: bool) -> None:
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError as error:
        raise ConfigurationError(f"proxy URL from {source} is invalid") from error
    scheme = parsed.scheme.casefold()
    if scheme == "socks":
        raise ConfigurationError(
            f"proxy URL from {source} uses unsupported scheme 'socks'; "
            "use socks5:// or socks5h:// explicitly"
        )
    if scheme in _SOCKS_PROXY_SCHEMES:
        if not socks_supported:
            raise ConfigurationError(
                f"proxy URL from {source} requires optional SOCKS support; "
                "install 'httpx[socks]' or use an HTTP proxy"
            )
    elif scheme not in _HTTP_PROXY_SCHEMES:
        rendered = scheme or "(missing)"
        raise ConfigurationError(f"proxy URL from {source} uses unsupported scheme {rendered!r}")
    if hostname is None or any(character.isspace() for character in value):
        raise ConfigurationError(f"proxy URL from {source} is invalid")
    if parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise ConfigurationError(f"proxy URL from {source} must not contain a path or query")


def resolve_http_client_policy(
    *,
    proxy_mode: str,
    proxy_url_env: str | None = None,
    environ: Mapping[str, str] | None = None,
    socks_supported: bool = False,
) -> HttpClientPolicy:
    """Resolve and validate one provider's proxy policy without exposing its URL.

    解析并验证一个 Provider 的代理策略,不暴露代理 URL."""

    source: Mapping[str, str] = {} if environ is None else environ
    if proxy_mode == "direct":
        if proxy_url_env is not None:
            raise ConfigurationError(
                "provider proxy environment variable requires proxy_mode 'explicit'"
            )
        return HttpClientPolicy(trust_env=False)
    if proxy_mode == "explicit":
        if not proxy_url_env:
            raise ConfigurationError("provider explicit proxy mode requires proxy_url_env")
        proxy_url = source.get(proxy_url_env, "").strip()
        if not proxy_url:
            raise ConfigurationError(
                f"proxy URL is missing; set environment variable {proxy_url_env}"
            )
        _validate_proxy_url(
            proxy_url,
            source=proxy_url_env,
            socks_supported=socks_supported,
        )
        return HttpClientPolicy(
            trust_env=False,
            proxy_url=proxy_url,
            redaction_values=_proxy_redaction_values(proxy_url),
        )
    if proxy_mode != "environment":
        raise ConfigurationError(
            "provider proxy_mode must be 'environment', 'direct', or 'explicit'"
        )
    if proxy_url_env is not None:
        raise ConfigurationError(
            "provider proxy environment variable requires proxy_mode 'explicit'"
        )

    configured = _proxy_environment_values(source)
    redactions: list[str] = []
    for name, proxy_url in configured:
        _validate_proxy_url(proxy_url, source=name, socks_supported=socks_supported)
        redactions.extend(_proxy_redaction_values(proxy_url))
    return HttpClientPolicy(trust_env=True, redaction_values=tuple(redactions))


@dataclass(frozen=True, slots=True)
class ProviderProfile:
    name: str
    protocol: str
    model: str
    base_url: str
    dialect: str = "standard"
    service_id: str | None = None
    auth: str = "env"
    api_key_env: str | None = None
    timeout_seconds: float = 120.0
    context_window_tokens: int | None = None
    max_output_tokens: int = 8192
    builtin_tools: tuple[str, ...] = ()
    capability_overrides: ModelCapabilitySet = field(
        default_factory=ModelCapabilitySet.all_unknown,
        repr=False,
    )
    native_context: str = "disabled"
    proxy_mode: str = "environment"
    proxy_url_env: str | None = None
    source: str = "native"
    unavailable_reason: str | None = None
    stored_api_key: str | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.name:
            raise ConfigurationError("provider profile name must not be empty")
        if self.protocol not in SUPPORTED_PROTOCOLS:
            raise ConfigurationError(f"unsupported provider protocol: {self.protocol}")
        if self.dialect not in SUPPORTED_DIALECTS:
            raise ConfigurationError(f"unsupported provider dialect: {self.dialect}")
        if self.dialect == "xai" and self.protocol != "openai-responses":
            raise ConfigurationError("xAI dialect requires protocol 'openai-responses'")
        if self.dialect == "deepseek-v4" and self.protocol != "openai-chat":
            raise ConfigurationError("DeepSeek V4 dialect requires protocol 'openai-chat'")
        if self.service_id is not None and not self.service_id.strip():
            raise ConfigurationError("provider service_id must not be empty")
        if not isinstance(self.capability_overrides, ModelCapabilitySet):
            raise ConfigurationError("provider capability overrides must be canonical")
        if not self.model:
            raise ConfigurationError(f"provider profile {self.name!r} requires an explicit model")
        if not self.base_url:
            raise ConfigurationError(f"provider profile {self.name!r} requires a base_url")
        _validate_base_url(self.base_url)
        protocol_status = DEFAULT_PROVIDER_SERVICE_CATALOG.protocol_support_for_profile(
            service_id=self.service_id,
            protocol=self.protocol,
            dialect=self.dialect,
            base_url=self.base_url,
            model=self.model,
        )
        if protocol_status is ProtocolSupportStatus.UNSUPPORTED:
            raise ConfigurationError(
                f"provider service {self.service_id!r} does not document protocol "
                f"{self.protocol!r} for model {self.model!r}"
            )
        if self.auth not in SUPPORTED_AUTH:
            raise ConfigurationError(f"unsupported provider authentication mode: {self.auth}")
        if self.auth == "env" and not self.api_key_env:
            raise ConfigurationError(
                f"provider profile {self.name!r} requires api_key_env for env authentication"
            )
        if self.auth == "stored" and not self.stored_api_key and self.unavailable_reason is None:
            raise ConfigurationError(f"provider profile {self.name!r} requires a stored API key")
        if self.auth != "stored" and self.stored_api_key is not None:
            raise ConfigurationError("stored API keys require stored authentication")
        if self.auth == "proxy-managed" and not _is_loopback_url(self.base_url):
            raise ConfigurationError("proxy-managed authentication requires an HTTP loopback URL")
        if self.timeout_seconds <= 0:
            raise ConfigurationError("provider timeout_seconds must be positive")
        if self.context_window_tokens is not None and self.context_window_tokens <= 0:
            raise ConfigurationError("provider context_window_tokens must be positive")
        if self.max_output_tokens <= 0:
            raise ConfigurationError("provider max_output_tokens must be positive")
        if self.native_context not in SUPPORTED_NATIVE_CONTEXT:
            raise ConfigurationError("provider native_context must be 'disabled' or 'profile'")
        if self.proxy_mode not in SUPPORTED_PROXY_MODES:
            raise ConfigurationError(
                "provider proxy_mode must be 'environment', 'direct', or 'explicit'"
            )
        if self.proxy_mode == "explicit" and not self.proxy_url_env:
            raise ConfigurationError("provider explicit proxy mode requires proxy_url_env")
        if self.proxy_mode != "explicit" and self.proxy_url_env is not None:
            raise ConfigurationError("provider proxy_url_env requires proxy_mode 'explicit'")
        if len(set(self.builtin_tools)) != len(self.builtin_tools):
            raise ConfigurationError("provider builtin_tools must not contain duplicates")
        if self.dialect == "xai":
            unsupported = sorted(set(self.builtin_tools) - _XAI_BUILTIN_TOOLS)
            if unsupported:
                names = ", ".join(repr(name) for name in unsupported)
                raise ConfigurationError(f"unsupported xAI builtin_tools: {names}")
        elif self.protocol == "openai-responses":
            unsupported = sorted(set(self.builtin_tools) - _OPENAI_RESPONSES_BUILTIN_TOOLS)
            if unsupported:
                names = ", ".join(repr(name) for name in unsupported)
                raise ConfigurationError(f"unsupported OpenAI Responses builtin_tools: {names}")
        elif self.protocol == "anthropic-messages":
            unsupported = sorted(set(self.builtin_tools) - _ANTHROPIC_BUILTIN_TOOLS)
            if unsupported:
                names = ", ".join(repr(name) for name in unsupported)
                raise ConfigurationError(f"unsupported Anthropic builtin_tools: {names}")
        elif self.protocol == "gemini-interactions":
            unsupported = sorted(set(self.builtin_tools) - _GEMINI_INTERACTIONS_BUILTIN_TOOLS)
            if unsupported:
                names = ", ".join(repr(name) for name in unsupported)
                raise ConfigurationError(f"unsupported Gemini Interactions builtin_tools: {names}")
        elif self.builtin_tools:
            raise ConfigurationError("provider builtin_tools require dialect 'xai'")

    @property
    def available(self) -> bool:
        return self.unavailable_reason is None and self.auth != "unsupported-inline"

    @property
    def context_affinity(self) -> str | None:
        if self.native_context != "profile":
            return None
        identity = "\0".join(
            (
                self.name,
                self.service_id or "",
                self.protocol,
                self.dialect,
                _canonical_url(self.base_url),
                self.model,
            )
        )
        return f"profile-v1:{hashlib.sha256(identity.encode()).hexdigest()}"

    def upstream_capabilities(self) -> ModelCapabilitySet:
        """Resolve only service, protocol, and model capability facts."""

        return DEFAULT_PROVIDER_SERVICE_CATALOG.upstream_capabilities_for_profile(
            service_id=self.service_id,
            protocol=self.protocol,
            dialect=self.dialect,
            base_url=self.base_url,
            model=self.model,
        )

    def capability_resolution(
        self,
        implementation: ModelCapabilitySet | None = None,
    ) -> CapabilityResolution:
        """Resolve trusted adapter evidence for this profile."""

        return DEFAULT_PROVIDER_SERVICE_CATALOG.capability_resolution_for_profile(
            service_id=self.service_id,
            protocol=self.protocol,
            dialect=self.dialect,
            base_url=self.base_url,
            model=self.model,
            implementation=implementation,
            configuration=self.capability_overrides,
        )

    def effective_capabilities(
        self,
        implementation: ModelCapabilitySet | None = None,
    ) -> ModelCapabilitySet:
        """Return executable capabilities, failing closed without adapter evidence."""

        return self.capability_resolution(implementation).effective

    @property
    def kind(self) -> str:
        """Legacy adapter name retained for inspection and downstream compatibility.

        保留旧适配器名称,用于 inspect 和下游兼容."""

        if self.protocol == "openai-chat":
            return "openai-compatible"
        if self.protocol == "openai-responses" and self.dialect == "xai":
            return "xai-responses"
        if self.protocol == "openai-responses":
            return "openai-responses"
        if self.protocol == "anthropic-messages":
            return "anthropic"
        return "gemini"

    def api_key(self, environ: Mapping[str, str] | None = None) -> str:
        if not self.available:
            raise ConfigurationError(
                self.unavailable_reason or f"provider profile {self.name!r} is not available"
            )
        if self.auth == "proxy-managed":
            return "PROXY_MANAGED"
        if self.auth == "stored":
            assert self.stored_api_key is not None
            return self.stored_api_key
        assert self.api_key_env is not None
        source: Mapping[str, str] = {} if environ is None else environ
        value = source.get(self.api_key_env, "").strip()
        if not value:
            raise ConfigurationError(
                f"model credential is missing; set environment variable {self.api_key_env}"
            )
        return value

    def http_client_policy(
        self,
        environ: Mapping[str, str] | None = None,
        *,
        socks_supported: bool = False,
    ) -> HttpClientPolicy:
        return resolve_http_client_policy(
            proxy_mode=self.proxy_mode,
            proxy_url_env=self.proxy_url_env,
            environ=environ,
            socks_supported=socks_supported,
        )

    def redacted_dict(self, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
        env: Mapping[str, str] = {} if environ is None else environ
        credential_configured = (
            self.auth == "proxy-managed"
            or (self.auth == "stored" and bool(self.stored_api_key))
            or bool(self.api_key_env and env.get(self.api_key_env))
        )
        proxy_url_configured = (
            bool(env.get(self.proxy_url_env, ""))
            if self.proxy_mode == "explicit" and self.proxy_url_env is not None
            else bool(_proxy_environment_values(env))
            if self.proxy_mode == "environment"
            else False
        )
        capability_resolution = self.capability_resolution()
        return {
            "name": self.name,
            "protocol": self.protocol,
            "dialect": self.dialect,
            "service_id": self.service_id,
            "kind": self.kind,
            "model": self.model,
            "base_url": self.base_url,
            "auth": self.auth,
            "api_key_env": self.api_key_env,
            "credential_configured": credential_configured,
            "timeout_seconds": self.timeout_seconds,
            "context_window_tokens": self.context_window_tokens,
            "max_output_tokens": self.max_output_tokens,
            "builtin_tools": list(self.builtin_tools),
            "capabilities": capability_resolution.effective.to_mapping(),
            "capability_provenance": capability_resolution.to_mapping(),
            "native_context": self.native_context,
            "proxy_mode": self.proxy_mode,
            "proxy_url_env": self.proxy_url_env,
            "proxy_url_configured": proxy_url_configured,
            "source": self.source,
            "available": self.available,
            "unavailable_reason": self.unavailable_reason,
        }


@dataclass(frozen=True, slots=True)
class AppConfig:
    cwd: Path
    state_dir: Path
    providers: Mapping[str, ProviderProfile]
    default_provider: str | None
    selected_provider: str | None
    sandbox_profile: SandboxProfile = SandboxProfile.OFF
    sandbox_profile_source: str = "default"
    fallback_providers: tuple[str, ...] = ()
    loaded_files: tuple[Path, ...] = ()
    routes: Mapping[RuntimeRole, ModelRoute] = field(default_factory=dict)
    web_search_mode: WebSearchMode = WebSearchMode.AUTO
    web_fetch_mode: WebFetchMode = WebFetchMode.DISABLED
    language_servers: Mapping[str, LanguageServerProfile] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "providers", MappingProxyType(dict(self.providers)))
        language_servers = dict(self.language_servers)
        if any(
            not isinstance(name, str)
            or not isinstance(profile, LanguageServerProfile)
            or profile.name != name
            for name, profile in language_servers.items()
        ):
            raise ConfigurationError("application language-server profiles are invalid")
        object.__setattr__(self, "language_servers", MappingProxyType(language_servers))
        normalized_routes = dict(self.routes)
        if any(not isinstance(role, RuntimeRole) for role in normalized_routes):
            raise ConfigurationError("application routes must use canonical runtime roles")
        if any(
            not isinstance(route, ModelRoute) or route.role is not role
            for role, route in normalized_routes.items()
        ):
            raise ConfigurationError("application routes must contain matching canonical routes")
        object.__setattr__(self, "routes", MappingProxyType(normalized_routes))
        if not isinstance(self.web_search_mode, WebSearchMode):
            raise ConfigurationError("application web_search_mode must be canonical")
        if not isinstance(self.web_fetch_mode, WebFetchMode):
            raise ConfigurationError("application web_fetch_mode must be canonical")

    @property
    def provider(self) -> ProviderProfile:
        if self.selected_provider is None:
            raise ConfigurationError(
                "no model provider is configured; add [providers.<name>] and "
                "[routing] default, set NEURO_CODE_PROVIDER, or enable a CC Switch profile"
            )
        try:
            return self.providers[self.selected_provider]
        except KeyError as error:
            raise ConfigurationError(
                f"selected provider profile does not exist: {self.selected_provider}"
            ) from error

    @property
    def main_route(self) -> ModelRoute:
        """Return the canonical MAIN route, projecting legacy routing fields."""

        explicit = self.routes.get(RuntimeRole.MAIN)
        if explicit is not None:
            return explicit
        provider = self.provider
        return ModelRoute(
            RuntimeRole.MAIN,
            provider.name,
            provider.model,
            tuple(name for name in self.fallback_providers if name != provider.name),
        )

    @property
    def web_search_route(self) -> ModelRoute | None:
        return self.routes.get(RuntimeRole.WEB_SEARCH)

    def route(self, role: RuntimeRole) -> ModelRoute | None:
        if role is RuntimeRole.MAIN:
            return self.main_route
        return self.web_search_route

    @property
    def protected_environment_variables(self) -> frozenset[str]:
        names = set(_PROXY_ENVIRONMENT_VARIABLES)
        for profile in self.providers.values():
            if profile.api_key_env is not None:
                names.add(profile.api_key_env)
            if profile.proxy_url_env is not None:
                names.add(profile.proxy_url_env)
        for lsp_profile in self.language_servers.values():
            names.update(lsp_profile.environment)
        return frozenset(name.casefold() for name in names)

    def redaction_values(self, environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
        """Return credential values that must never cross a tool-result boundary.

        返回绝不能跨越工具结果边界的凭据值."""

        env: Mapping[str, str] = {} if environ is None else environ
        values: list[str] = []
        for profile in self.providers.values():
            if profile.stored_api_key:
                values.append(profile.stored_api_key)
            if profile.api_key_env and env.get(profile.api_key_env):
                values.append(env[profile.api_key_env])
            if profile.proxy_url_env and env.get(profile.proxy_url_env):
                values.append(env[profile.proxy_url_env])
        for lsp_profile in self.language_servers.values():
            values.extend(lsp_profile.environment.values())
        return tuple(dict.fromkeys(value for value in values if value))

    def redacted_dict(self, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
        profiles = {
            name: profile.redacted_dict(environ) for name, profile in self.providers.items()
        }
        selected = profiles.get(self.selected_provider or "")
        return {
            "cwd": str(self.cwd),
            "state_dir": str(self.state_dir),
            "routing": {
                "default": self.default_provider,
                "selected": self.selected_provider,
                "fallbacks": list(self.fallback_providers),
                "routes": {role.value: route.to_dict() for role, route in self.routes.items()},
            },
            "sandbox": {
                "profile": self.sandbox_profile.value,
                "source": self.sandbox_profile_source,
            },
            "web_search": {"mode": self.web_search_mode.value},
            "web_fetch": {"mode": self.web_fetch_mode.value},
            "lsp": {
                "servers": {
                    name: {
                        "language": profile.language,
                        "command": list(profile.command),
                        "extensions": list(profile.extensions),
                        "root_markers": list(profile.root_markers),
                        "environment_names": sorted(profile.environment),
                        "enabled": profile.enabled,
                    }
                    for name, profile in self.language_servers.items()
                }
            },
            "provider": selected,
            "providers": profiles,
            "loaded_files": [str(path) for path in self.loaded_files],
        }


def override_provider(
    config: AppConfig,
    *,
    provider: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
) -> AppConfig:
    selected = provider or config.selected_provider
    if selected is None:
        raise ConfigurationError(
            "no model provider is configured; add [providers.<name>] and "
            "[routing] default, set NEURO_CODE_PROVIDER, or enable a CC Switch profile"
        )
    if selected not in config.providers:
        raise ConfigurationError(f"selected provider profile does not exist: {selected}")
    profiles = dict(config.providers)
    profile = profiles[selected]
    profiles[selected] = replace(
        profile,
        model=model or profile.model,
        base_url=(base_url or profile.base_url).rstrip("/"),
        context_window_tokens=(
            profile.context_window_tokens if model is None or model == profile.model else None
        ),
    )
    return replace(config, providers=profiles, selected_provider=selected)


def override_sandbox(config: AppConfig, profile: str | None) -> AppConfig:
    if profile is None:
        return config
    try:
        selected = SandboxProfile.parse(profile)
    except ValueError as error:
        raise ConfigurationError(str(error)) from error
    return replace(config, sandbox_profile=selected, sandbox_profile_source="cli")


def pin_resumed_sandbox(
    config: AppConfig,
    saved_profile: SandboxProfile | None,
) -> AppConfig:
    """Restore a session's fixed sandbox profile without silent CLI/env changes.

    恢复会话固定的沙箱配置档案,不静默应用 CLI 或环境变量变化."""

    if saved_profile is None:
        return config
    if (
        config.sandbox_profile_source in {"cli", "environment"}
        and config.sandbox_profile is not saved_profile
    ):
        raise ConfigurationError(
            "resumed session sandbox profile conflict: "
            f"requested {config.sandbox_profile.value!r}, "
            f"but the session was created with {saved_profile.value!r}"
        )
    return replace(
        config,
        sandbox_profile=saved_profile,
        sandbox_profile_source="session",
    )
