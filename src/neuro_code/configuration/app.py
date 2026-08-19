"""Canonical application configuration loading and provider profile models.

定义规范的应用配置加载逻辑和 Provider 配置档案模型."""

from __future__ import annotations

import hashlib
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from importlib.util import find_spec
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

from neuro_code.application.ports.http import HttpClientPolicy
from neuro_code.application.ports.model import CapabilityResolution, ModelCapabilitySet
from neuro_code.application.ports.provider_services import (
    DEFAULT_PROVIDER_SERVICE_CATALOG,
    SUPPORTED_DIALECTS,
    SUPPORTED_PROTOCOLS,
)
from neuro_code.application.ports.routing import ModelRoute, RuntimeRole
from neuro_code.application.ports.web_search import WebSearchMode
from neuro_code.configuration.managed_provider_settings import (
    load_managed_provider_settings as _load_managed_provider_settings,
)
from neuro_code.configuration.provider_dialects import resolve_legacy_dialect
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
    "load_config",
    "override_provider",
    "override_sandbox",
    "pin_resumed_sandbox",
    "resolve_http_client_policy",
]

_LEGACY_KINDS: dict[str, tuple[str, str]] = {
    "openai-compatible": ("openai-chat", "standard"),
    "xai-responses": ("openai-responses", "xai"),
    "anthropic": ("anthropic-messages", "standard"),
    "gemini": ("gemini-generate-content", "standard"),
}
_LEGACY_SERVICE_IDS = {
    "openai-compatible": "generic-openai-compatible",
    "xai-responses": "xai",
    "anthropic": "anthropic",
    "gemini": "google-ai-studio",
}
_LEGACY_DEFAULTS: dict[str, tuple[str, str, str]] = {
    "openai-compatible": ("https://api.x.ai/v1", "XAI_API_KEY", ""),
    "xai-responses": ("https://api.x.ai/v1", "XAI_API_KEY", ""),
    "anthropic": ("https://api.anthropic.com", "ANTHROPIC_API_KEY", ""),
    "gemini": (
        "https://generativelanguage.googleapis.com/v1beta",
        "GEMINI_API_KEY",
        "",
    ),
}
_CC_SWITCH_BACKENDS = {
    "responses": "openai-responses",
    "chat_completions": "openai-chat",
    "messages": "anthropic-messages",
}
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


def _validate_proxy_url(value: str, *, source: str) -> None:
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
        if find_spec("socksio") is None:
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
) -> HttpClientPolicy:
    """Resolve and validate one provider's proxy policy without exposing its URL.

    解析并验证一个 Provider 的代理策略,不暴露代理 URL."""

    source = os.environ if environ is None else environ
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
        _validate_proxy_url(proxy_url, source=proxy_url_env)
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
        _validate_proxy_url(proxy_url, source=name)
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
            (self.name, self.protocol, self.dialect, _canonical_url(self.base_url), self.model)
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
        source = os.environ if environ is None else environ
        value = source.get(self.api_key_env, "").strip()
        if not value:
            raise ConfigurationError(
                f"model credential is missing; set environment variable {self.api_key_env}"
            )
        return value

    def http_client_policy(self, environ: Mapping[str, str] | None = None) -> HttpClientPolicy:
        return resolve_http_client_policy(
            proxy_mode=self.proxy_mode,
            proxy_url_env=self.proxy_url_env,
            environ=environ,
        )

    def redacted_dict(self, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
        env = os.environ if environ is None else environ
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

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "cwd", self.cwd.expanduser().resolve(strict=False))
            object.__setattr__(self, "state_dir", self.state_dir.expanduser().resolve(strict=False))
        except (AttributeError, OSError, RuntimeError) as error:
            raise ConfigurationError("application filesystem paths must be resolvable") from error
        object.__setattr__(self, "providers", MappingProxyType(dict(self.providers)))
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
        return frozenset(name.casefold() for name in names)

    def redaction_values(self, environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
        """Return credential values that must never cross a tool-result boundary.

        返回绝不能跨越工具结果边界的凭据值."""

        env = os.environ if environ is None else environ
        values: list[str] = []
        for profile in self.providers.values():
            if profile.stored_api_key:
                values.append(profile.stored_api_key)
            if profile.api_key_env and env.get(profile.api_key_env):
                values.append(env[profile.api_key_env])
            if profile.proxy_url_env and env.get(profile.proxy_url_env):
                values.append(env[profile.proxy_url_env])
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
            "provider": selected,
            "providers": profiles,
            "loaded_files": [str(path) for path in self.loaded_files],
        }


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as file:
            loaded = tomllib.load(file)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigurationError(f"cannot load configuration {path}: {error}") from error
    return loaded


def _deep_merge(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, Mapping):
            merged[key] = _deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def _string(value: object, default: str = "") -> str:
    return value.strip() if isinstance(value, str) and value.strip() else default


def _number(value: object, *, name: str, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigurationError(f"provider {name} must be a number")
    return float(value)


def _integer(value: object, *, name: str, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigurationError(f"provider {name} must be an integer")
    return value


def _string_array(value: object, *, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConfigurationError(f"provider {name} must be a TOML array")
    if any(not isinstance(item, str) or not item for item in value):
        raise ConfigurationError(f"provider {name} entries must be non-empty strings")
    return tuple(value)


def _capability_overrides(value: object) -> ModelCapabilitySet:
    if value is None:
        return ModelCapabilitySet.all_unknown()
    if not isinstance(value, Mapping):
        raise ConfigurationError("provider capabilities must be a TOML table")
    if any(
        not isinstance(name, str) or not isinstance(status, str) for name, status in value.items()
    ):
        raise ConfigurationError("provider capabilities must map names to statuses")
    try:
        return ModelCapabilitySet.from_mapping(value)
    except (TypeError, ValueError) as error:
        raise ConfigurationError("provider capabilities contain an unsupported value") from error


def _web_search_mode_from_data(data: Mapping[str, object]) -> WebSearchMode:
    raw = data.get("web_search")
    if raw is None:
        return WebSearchMode.AUTO
    if not isinstance(raw, Mapping):
        raise ConfigurationError("[web_search] must be a TOML table")
    mode = _string(raw.get("mode"), WebSearchMode.AUTO.value)
    try:
        return WebSearchMode(mode)
    except ValueError as error:
        values = ", ".join(item.value for item in WebSearchMode)
        raise ConfigurationError(f"web_search mode must be one of: {values}") from error


def _sandbox_profile_from_data(data: Mapping[str, object]) -> SandboxProfile | None:
    raw_sandbox = data.get("sandbox")
    if raw_sandbox is None:
        return None
    if not isinstance(raw_sandbox, Mapping):
        raise ConfigurationError("[sandbox] must be a TOML table")
    raw_profile = raw_sandbox.get("profile")
    if raw_profile is None:
        return None
    if not isinstance(raw_profile, str) or not raw_profile.strip():
        raise ConfigurationError("sandbox profile must be a non-empty string")
    try:
        return SandboxProfile.parse(raw_profile)
    except ValueError as error:
        raise ConfigurationError(str(error)) from error


def _sandbox_profile_from_environment(environ: Mapping[str, str]) -> SandboxProfile | None:
    raw_profile = environ.get("NEURO_CODE_SANDBOX", "").strip()
    if not raw_profile:
        return None
    try:
        return SandboxProfile.parse(raw_profile)
    except ValueError as error:
        raise ConfigurationError(str(error)) from error


def _native_profile(
    name: str,
    raw: Mapping[str, object],
    *,
    legacy_table: bool = False,
    stored_api_key: str | None = None,
) -> ProviderProfile:
    legacy_kind = _string(raw.get("kind"))
    if legacy_table and not legacy_kind and not _string(raw.get("protocol")):
        legacy_kind = "openai-compatible"
    if legacy_kind:
        try:
            protocol, default_dialect = _LEGACY_KINDS[legacy_kind]
            default_url, default_env, default_model = _LEGACY_DEFAULTS[legacy_kind]
        except KeyError as error:
            raise ConfigurationError(f"unsupported provider kind: {legacy_kind}") from error
    else:
        protocol = _string(raw.get("protocol"))
        default_dialect = "standard"
        default_url = ""
        default_env = ""
        default_model = ""
    protocol = _string(raw.get("protocol"), protocol)
    model = _string(raw.get("model"), default_model)
    base_url = _string(raw.get("base_url"), default_url).rstrip("/")
    raw_dialect = raw.get("dialect")
    if raw_dialect is not None and (not isinstance(raw_dialect, str) or not raw_dialect.strip()):
        raise ConfigurationError("provider dialect must be a non-empty string")
    explicit_dialect = raw_dialect.strip() if isinstance(raw_dialect, str) else None
    dialect = resolve_legacy_dialect(
        explicit_dialect=explicit_dialect,
        provider_name=name,
        protocol=protocol,
        model=model,
        base_url=base_url,
        legacy_default_dialect=default_dialect,
    )
    if protocol == "openai-responses" and dialect == "xai":
        default_url = default_url or "https://api.x.ai/v1"
        default_env = default_env or "XAI_API_KEY"
    if not protocol:
        raise ConfigurationError(f"provider profile {name!r} requires protocol (or legacy kind)")
    auth = _string(raw.get("auth"), "env")
    api_key_env = _string(raw.get("api_key_env"), default_env) or None
    native_context = _string(
        raw.get("native_context"),
        "profile" if dialect == "xai" or protocol == "gemini-interactions" else "disabled",
    )
    unavailable_reason = (
        f"managed provider profile {name!r} is missing its stored API key"
        if auth == "stored" and stored_api_key is None
        else None
    )
    configured_service = _string(raw.get("service_id"), _string(raw.get("service")))
    return ProviderProfile(
        name=name,
        protocol=protocol,
        dialect=dialect,
        service_id=configured_service or _LEGACY_SERVICE_IDS.get(legacy_kind),
        model=model,
        base_url=base_url,
        auth=auth,
        api_key_env=api_key_env,
        timeout_seconds=_number(raw.get("timeout_seconds"), name="timeout_seconds", default=120.0),
        context_window_tokens=(
            None
            if raw.get("context_window_tokens") is None
            else _integer(
                raw.get("context_window_tokens"),
                name="context_window_tokens",
                default=0,
            )
        ),
        max_output_tokens=_integer(
            raw.get("max_output_tokens"), name="max_output_tokens", default=8192
        ),
        builtin_tools=_string_array(raw.get("builtin_tools"), name="builtin_tools"),
        capability_overrides=_capability_overrides(raw.get("capabilities")),
        native_context=native_context,
        proxy_mode=_string(raw.get("proxy_mode"), "environment"),
        proxy_url_env=_string(raw.get("proxy_url_env")) or None,
        source="legacy" if legacy_kind else "native",
        unavailable_reason=unavailable_reason,
        stored_api_key=stored_api_key,
    )


def _legacy_model_profile(raw: Mapping[str, object]) -> ProviderProfile:
    env_value = raw.get("env_key", "XAI_API_KEY")
    if isinstance(env_value, list):
        env_value = next((item for item in env_value if isinstance(item, str)), "XAI_API_KEY")
    model = _string(raw.get("model"))
    base_url = _string(raw.get("base_url"), "https://api.x.ai/v1").rstrip("/")
    dialect = resolve_legacy_dialect(
        explicit_dialect=None,
        provider_name="default",
        protocol="openai-chat",
        model=model,
        base_url=base_url,
    )
    return ProviderProfile(
        name="default",
        protocol="openai-chat",
        dialect=dialect,
        model=model,
        base_url=base_url,
        auth="env",
        api_key_env=_string(env_value, "XAI_API_KEY"),
        source="legacy-config",
    )


def _cc_switch_profile(alias: str, raw: Mapping[str, object]) -> ProviderProfile:
    name = f"cc-switch:{alias}"
    backend = _string(raw.get("api_backend"), "responses")
    try:
        protocol = _CC_SWITCH_BACKENDS[backend]
    except KeyError as error:
        raise ConfigurationError(
            f"CC Switch profile {alias!r} has unsupported api_backend {backend!r}"
        ) from error
    base_url = _string(raw.get("base_url")).rstrip("/")
    model = _string(raw.get("model"))
    env_value = raw.get("env_key")
    if isinstance(env_value, list):
        env_value = next((item for item in env_value if isinstance(item, str) and item), None)
    api_key_env = _string(env_value) or None
    inline_key = _string(raw.get("api_key"))
    auth = "env"
    unavailable_reason: str | None = None
    if api_key_env:
        pass
    elif inline_key == "PROXY_MANAGED" and _is_loopback_url(base_url):
        auth = "proxy-managed"
    else:
        auth = "unsupported-inline"
        unavailable_reason = (
            f"CC Switch profile {alias!r} uses an inline API key; configure env_key "
            "or enable CC Switch proxy takeover instead"
        )
    dialect = resolve_legacy_dialect(
        explicit_dialect=None,
        provider_name=name,
        protocol=protocol,
        model=model,
        base_url=base_url,
    )
    return ProviderProfile(
        name=name,
        protocol=protocol,
        dialect=dialect,
        model=model,
        base_url=base_url,
        auth=auth,
        api_key_env=api_key_env,
        native_context="disabled",
        source="cc-switch",
        unavailable_reason=unavailable_reason,
    )


def _profiles_from_data(
    data: Mapping[str, object],
    *,
    stored_api_keys: Mapping[str, str] | None = None,
) -> tuple[dict[str, ProviderProfile], str | None]:
    profiles: dict[str, ProviderProfile] = {}
    raw_profiles = data.get("providers", {})
    if not isinstance(raw_profiles, Mapping):
        raise ConfigurationError("[providers] must be a TOML table")
    for name, raw in raw_profiles.items():
        if not isinstance(name, str) or not isinstance(raw, Mapping):
            raise ConfigurationError("each [providers.<name>] entry must be a TOML table")
        profiles[name] = _native_profile(
            name,
            raw,
            stored_api_key=(stored_api_keys or {}).get(name),
        )

    raw_model_profiles = data.get("model", {})
    legacy_provider = data.get("provider", {})
    if not isinstance(legacy_provider, Mapping):
        raise ConfigurationError("[provider] must be a TOML table")
    legacy_default = legacy_provider.get("default")
    if legacy_default is not None:
        if not isinstance(legacy_default, Mapping):
            raise ConfigurationError("[provider.default] must be a TOML table")
        merged_legacy = dict(legacy_default)
        legacy_kind = _string(merged_legacy.get("kind"), "openai-compatible")
        old_default = (
            raw_model_profiles.get("default") if isinstance(raw_model_profiles, Mapping) else None
        )
        if legacy_kind == "openai-compatible" and isinstance(old_default, Mapping):
            for field in ("model", "base_url"):
                if field not in merged_legacy and field in old_default:
                    merged_legacy[field] = old_default[field]
            if "api_key_env" not in merged_legacy and "env_key" in old_default:
                merged_legacy["api_key_env"] = old_default["env_key"]
        profiles.setdefault(
            "default",
            _native_profile("default", merged_legacy, legacy_table=True),
        )

    raw_models = data.get("models", {})
    cc_default: str | None = None
    if isinstance(raw_models, Mapping):
        cc_alias = _string(raw_models.get("default"))
        if cc_alias and isinstance(raw_model_profiles, Mapping):
            cc_raw = raw_model_profiles.get(cc_alias)
            if isinstance(cc_raw, Mapping):
                cc_profile = _cc_switch_profile(cc_alias, cc_raw)
                profiles.setdefault(cc_profile.name, cc_profile)
                cc_default = cc_profile.name

    if "default" not in profiles and isinstance(raw_model_profiles, Mapping):
        old_default = raw_model_profiles.get("default")
        if isinstance(old_default, Mapping):
            profiles["default"] = _legacy_model_profile(old_default)
    return profiles, cc_default


def _role_route_from_data(
    role: RuntimeRole,
    raw: object,
    providers: Mapping[str, ProviderProfile],
) -> ModelRoute:
    if not isinstance(raw, Mapping):
        raise ConfigurationError(f"[routing.{role.value}] must be a TOML table")
    profile_name = _string(raw.get("profile"), _string(raw.get("provider")))
    if not profile_name:
        raise ConfigurationError(f"[routing.{role.value}] requires profile")
    profile = providers.get(profile_name)
    if profile is None:
        raise ConfigurationError(
            f"{role.value} route provider profile does not exist: {profile_name}"
        )
    raw_fallbacks = raw.get("fallbacks", [])
    if not isinstance(raw_fallbacks, list):
        raise ConfigurationError(f"routing.{role.value} fallbacks must be a TOML array")
    if any(not isinstance(name, str) or not name.strip() for name in raw_fallbacks):
        raise ConfigurationError(f"routing.{role.value} fallback entries must be non-empty strings")
    fallback_profiles = tuple(raw_fallbacks)
    if len(set(fallback_profiles)) != len(fallback_profiles):
        raise ConfigurationError(f"routing.{role.value} fallbacks must not contain duplicates")
    missing = [name for name in fallback_profiles if name not in providers]
    if missing:
        names = ", ".join(repr(name) for name in missing)
        raise ConfigurationError(f"routing.{role.value} fallback profiles do not exist: {names}")
    if "execution_path" in raw:
        raise ConfigurationError(
            f"routing.{role.value} execution_path is not part of the generic route contract"
        )
    return ModelRoute(
        role=role,
        provider_profile=profile_name,
        model=_string(raw.get("model"), profile.model),
        fallback_profiles=fallback_profiles,
    )


def _routes_from_data(
    routing: Mapping[str, object],
    providers: Mapping[str, ProviderProfile],
) -> dict[RuntimeRole, ModelRoute]:
    routes: dict[RuntimeRole, ModelRoute] = {}
    for role in RuntimeRole:
        raw = routing.get(role.value)
        if raw is not None:
            routes[role] = _role_route_from_data(role, raw, providers)
    return routes


def load_config(
    cwd: Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> AppConfig:
    env = os.environ if environ is None else environ
    resolved_cwd = (cwd or Path.cwd()).expanduser().resolve()
    if home is not None:
        resolved_home: Path | None = home.expanduser().resolve()
    else:
        try:
            resolved_home = Path.home().resolve()
        except RuntimeError:
            resolved_home = None

    configured_state_dir = env.get("NEURO_CODE_HOME")
    if configured_state_dir:
        try:
            state_dir = Path(configured_state_dir).expanduser().resolve()
        except RuntimeError as error:
            raise ConfigurationError(f"cannot resolve NEURO_CODE_HOME: {error}") from error
    elif resolved_home is not None:
        state_dir = resolved_home / ".neuro-code"
    else:
        raise ConfigurationError("cannot determine user home; set NEURO_CODE_HOME explicitly")

    candidates: list[Path] = []
    cc_switch_config = _string(env.get("NEURO_CODE_CC_SWITCH_CONFIG"))
    if cc_switch_config:
        try:
            candidates.append(Path(cc_switch_config).expanduser().resolve())
        except RuntimeError as error:
            raise ConfigurationError(
                f"cannot resolve NEURO_CODE_CC_SWITCH_CONFIG: {error}"
            ) from error
    user_config_path = state_dir / "config.toml"
    project_config_path = resolved_cwd / ".neuro-code" / "config.toml"
    candidates.extend((user_config_path, project_config_path))
    data: dict[str, Any] = {}
    loaded_files: list[Path] = []
    user_data: Mapping[str, object] = {}
    project_data: Mapping[str, object] = {}
    for candidate in candidates:
        if candidate.is_file():
            loaded = _read_toml(candidate)
            data = _deep_merge(data, loaded)
            loaded_files.append(candidate)
            if candidate == user_config_path:
                user_data = loaded
            if candidate == project_config_path:
                project_data = loaded

    managed = _load_managed_provider_settings(state_dir)
    if managed.profiles:
        raw_provider_table = data.get("providers", {})
        if not isinstance(raw_provider_table, Mapping):
            raise ConfigurationError("[providers] must be a TOML table")
        managed_provider_table = dict(raw_provider_table)
        for managed_profile in managed.profiles:
            # Replace the complete same-name table. In particular, do not inherit a
            # workspace-controlled endpoint, proxy, or tool flag for a stored key.
            proxy_policy = managed_profile.effective_proxy_policy(managed.proxy_defaults)
            managed_provider: dict[str, object] = {
                "protocol": managed_profile.protocol,
                "dialect": managed_profile.dialect,
                "service_id": managed_profile.service_id,
                "model": managed_profile.model,
                "base_url": managed_profile.base_url,
                "auth": "stored",
                "proxy_mode": proxy_policy.mode,
                "capabilities": managed_profile.capability_overrides.to_mapping(
                    include_unknown=False
                ),
            }
            if managed_profile.context_window_tokens is not None:
                managed_provider["context_window_tokens"] = managed_profile.context_window_tokens
            if proxy_policy.proxy_url_env is not None:
                managed_provider["proxy_url_env"] = proxy_policy.proxy_url_env
            managed_provider_table[managed_profile.name] = managed_provider
        data = dict(data)
        data["providers"] = managed_provider_table
        if managed.default_provider is not None:
            raw_routing = data.get("routing", {})
            if not isinstance(raw_routing, Mapping):
                raise ConfigurationError("[routing] must be a TOML table")
            data["routing"] = {**raw_routing, "default": managed.default_provider}
        # User-managed profiles deliberately win name collisions so that a workspace
        # cannot redirect a private stored credential to a different endpoint.

    user_sandbox = _sandbox_profile_from_data(user_data)
    project_sandbox = _sandbox_profile_from_data(project_data)
    environment_sandbox = _sandbox_profile_from_environment(env)
    if environment_sandbox is not None:
        sandbox_profile = environment_sandbox
        sandbox_profile_source = "environment"
    elif user_sandbox is not None:
        # A workspace cannot weaken a profile explicitly selected in user state.
        sandbox_profile = user_sandbox
        sandbox_profile_source = "user"
    elif project_sandbox is not None:
        sandbox_profile = project_sandbox
        sandbox_profile_source = "project"
    else:
        sandbox_profile = SandboxProfile.OFF
        sandbox_profile_source = "default"

    providers, cc_default = _profiles_from_data(
        data,
        stored_api_keys={
            managed_profile.name: managed_profile.api_key
            for managed_profile in managed.profiles
            if managed_profile.api_key is not None
        },
    )
    routing = data.get("routing", {})
    if not isinstance(routing, Mapping):
        raise ConfigurationError("[routing] must be a TOML table")
    configured_default = _string(routing.get("default")) or None
    if configured_default is None:
        configured_default = "default" if "default" in providers else cc_default
    selected = _string(env.get("NEURO_CODE_PROVIDER")) or configured_default
    if selected is not None and selected not in providers:
        raise ConfigurationError(f"selected provider profile does not exist: {selected}")
    raw_fallbacks = routing.get("fallbacks", [])
    if not isinstance(raw_fallbacks, list):
        raise ConfigurationError("routing fallbacks must be a TOML array")
    if any(not isinstance(name, str) or not name.strip() for name in raw_fallbacks):
        raise ConfigurationError("routing fallbacks entries must be non-empty strings")
    fallback_providers = tuple(raw_fallbacks)
    if len(set(fallback_providers)) != len(fallback_providers):
        raise ConfigurationError("routing fallbacks must not contain duplicates")
    missing_fallbacks = [name for name in fallback_providers if name not in providers]
    if missing_fallbacks:
        names = ", ".join(repr(name) for name in missing_fallbacks)
        raise ConfigurationError(f"routing fallback profiles do not exist: {names}")
    if configured_default is not None and configured_default in fallback_providers:
        raise ConfigurationError("routing fallbacks must not include the default provider")

    if selected is not None:
        profile = providers[selected]
        model_override = _string(env.get("NEURO_CODE_MODEL"))
        base_url_override = _string(env.get("NEURO_CODE_BASE_URL"))
        if model_override or base_url_override:
            providers[selected] = replace(
                profile,
                model=model_override or profile.model,
                base_url=(base_url_override or profile.base_url).rstrip("/"),
            )

    routes = _routes_from_data(routing, providers)
    web_search_mode = _web_search_mode_from_data(data)

    return AppConfig(
        cwd=resolved_cwd,
        state_dir=state_dir,
        providers=providers,
        default_provider=configured_default,
        selected_provider=selected,
        sandbox_profile=sandbox_profile,
        sandbox_profile_source=sandbox_profile_source,
        fallback_providers=fallback_providers,
        loaded_files=tuple(loaded_files),
        routes=routes,
        web_search_mode=web_search_mode,
    )


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
