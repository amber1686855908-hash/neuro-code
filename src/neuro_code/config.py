from __future__ import annotations

import hashlib
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from importlib.util import find_spec
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

from neuro_code.errors import ConfigurationError
from neuro_code.ports.http import HttpClientPolicy

SUPPORTED_PROTOCOLS = frozenset(
    {
        "openai-chat",
        "openai-responses",
        "anthropic-messages",
        "gemini-generate-content",
    }
)
SUPPORTED_DIALECTS = frozenset({"standard", "xai"})
SUPPORTED_AUTH = frozenset({"env", "proxy-managed", "unsupported-inline"})
SUPPORTED_NATIVE_CONTEXT = frozenset({"disabled", "profile"})
SUPPORTED_PROXY_MODES = frozenset({"environment", "direct", "explicit"})

_LEGACY_KINDS: dict[str, tuple[str, str]] = {
    "openai-compatible": ("openai-chat", "standard"),
    "xai-responses": ("openai-responses", "xai"),
    "anthropic": ("anthropic-messages", "standard"),
    "gemini": ("gemini-generate-content", "standard"),
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


@dataclass(frozen=True, slots=True)
class ProviderProfile:
    name: str
    protocol: str
    model: str
    base_url: str
    dialect: str = "standard"
    auth: str = "env"
    api_key_env: str | None = None
    timeout_seconds: float = 120.0
    max_output_tokens: int = 8192
    builtin_tools: tuple[str, ...] = ()
    native_context: str = "disabled"
    proxy_mode: str = "environment"
    proxy_url_env: str | None = None
    source: str = "native"
    unavailable_reason: str | None = None

    def __post_init__(self) -> None:
        if not self.name:
            raise ConfigurationError("provider profile name must not be empty")
        if self.protocol not in SUPPORTED_PROTOCOLS:
            raise ConfigurationError(f"unsupported provider protocol: {self.protocol}")
        if self.dialect not in SUPPORTED_DIALECTS:
            raise ConfigurationError(f"unsupported provider dialect: {self.dialect}")
        if self.dialect == "xai" and self.protocol != "openai-responses":
            raise ConfigurationError("xAI dialect requires protocol 'openai-responses'")
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
        if self.auth == "proxy-managed" and not _is_loopback_url(self.base_url):
            raise ConfigurationError("proxy-managed authentication requires an HTTP loopback URL")
        if self.timeout_seconds <= 0:
            raise ConfigurationError("provider timeout_seconds must be positive")
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
        unsupported = sorted(set(self.builtin_tools) - _XAI_BUILTIN_TOOLS)
        if unsupported:
            names = ", ".join(repr(name) for name in unsupported)
            raise ConfigurationError(f"unsupported xAI builtin_tools: {names}")
        if self.builtin_tools and self.dialect != "xai":
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

    @property
    def kind(self) -> str:
        """Legacy adapter name retained for inspection and downstream compatibility."""

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
        assert self.api_key_env is not None
        source = os.environ if environ is None else environ
        value = source.get(self.api_key_env, "").strip()
        if not value:
            raise ConfigurationError(
                f"model credential is missing; set environment variable {self.api_key_env}"
            )
        return value

    def http_client_policy(self, environ: Mapping[str, str] | None = None) -> HttpClientPolicy:
        source = os.environ if environ is None else environ
        if self.proxy_mode == "direct":
            return HttpClientPolicy(trust_env=False)
        if self.proxy_mode == "explicit":
            assert self.proxy_url_env is not None
            proxy_url = source.get(self.proxy_url_env, "").strip()
            if not proxy_url:
                raise ConfigurationError(
                    f"proxy URL is missing; set environment variable {self.proxy_url_env}"
                )
            _validate_proxy_url(proxy_url, source=self.proxy_url_env)
            return HttpClientPolicy(
                trust_env=False,
                proxy_url=proxy_url,
                redaction_values=_proxy_redaction_values(proxy_url),
            )

        configured = _proxy_environment_values(source)
        redactions: list[str] = []
        for name, proxy_url in configured:
            _validate_proxy_url(proxy_url, source=name)
            redactions.extend(_proxy_redaction_values(proxy_url))
        return HttpClientPolicy(trust_env=True, redaction_values=tuple(redactions))

    def redacted_dict(self, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
        env = os.environ if environ is None else environ
        credential_configured = self.auth == "proxy-managed" or bool(
            self.api_key_env and env.get(self.api_key_env)
        )
        proxy_url_configured = (
            bool(env.get(self.proxy_url_env, ""))
            if self.proxy_mode == "explicit" and self.proxy_url_env is not None
            else bool(_proxy_environment_values(env))
            if self.proxy_mode == "environment"
            else False
        )
        return {
            "name": self.name,
            "protocol": self.protocol,
            "dialect": self.dialect,
            "kind": self.kind,
            "model": self.model,
            "base_url": self.base_url,
            "auth": self.auth,
            "api_key_env": self.api_key_env,
            "credential_configured": credential_configured,
            "timeout_seconds": self.timeout_seconds,
            "max_output_tokens": self.max_output_tokens,
            "builtin_tools": list(self.builtin_tools),
            "native_context": self.native_context,
            "proxy_mode": self.proxy_mode,
            "proxy_url_env": self.proxy_url_env,
            "proxy_url_configured": proxy_url_configured,
            "source": self.source,
            "available": self.available,
            "unavailable_reason": self.unavailable_reason,
        }


# Kept as an import-compatible name while callers migrate to ProviderProfile.
ProviderConfig = ProviderProfile


@dataclass(frozen=True, slots=True)
class AppConfig:
    cwd: Path
    state_dir: Path
    providers: Mapping[str, ProviderProfile]
    default_provider: str | None
    selected_provider: str | None
    fallback_providers: tuple[str, ...] = ()
    loaded_files: tuple[Path, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "providers", MappingProxyType(dict(self.providers)))

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
            },
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


def _native_profile(
    name: str,
    raw: Mapping[str, object],
    *,
    legacy_table: bool = False,
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
    dialect = _string(raw.get("dialect"), default_dialect)
    if protocol == "openai-responses" and dialect == "xai":
        default_url = default_url or "https://api.x.ai/v1"
        default_env = default_env or "XAI_API_KEY"
    if not protocol:
        raise ConfigurationError(f"provider profile {name!r} requires protocol (or legacy kind)")
    auth = _string(raw.get("auth"), "env")
    api_key_env = _string(raw.get("api_key_env"), default_env) or None
    native_context = _string(
        raw.get("native_context"),
        "profile" if dialect == "xai" else "disabled",
    )
    return ProviderProfile(
        name=name,
        protocol=protocol,
        dialect=dialect,
        model=_string(raw.get("model"), default_model),
        base_url=_string(raw.get("base_url"), default_url).rstrip("/"),
        auth=auth,
        api_key_env=api_key_env,
        timeout_seconds=_number(raw.get("timeout_seconds"), name="timeout_seconds", default=120.0),
        max_output_tokens=_integer(
            raw.get("max_output_tokens"), name="max_output_tokens", default=8192
        ),
        builtin_tools=_string_array(raw.get("builtin_tools"), name="builtin_tools"),
        native_context=native_context,
        proxy_mode=_string(raw.get("proxy_mode"), "environment"),
        proxy_url_env=_string(raw.get("proxy_url_env")) or None,
        source="legacy" if legacy_kind else "native",
    )


def _legacy_model_profile(raw: Mapping[str, object]) -> ProviderProfile:
    env_value = raw.get("env_key", "XAI_API_KEY")
    if isinstance(env_value, list):
        env_value = next((item for item in env_value if isinstance(item, str)), "XAI_API_KEY")
    return ProviderProfile(
        name="default",
        protocol="openai-chat",
        dialect="standard",
        model=_string(raw.get("model")),
        base_url=_string(raw.get("base_url"), "https://api.x.ai/v1").rstrip("/"),
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
    return ProviderProfile(
        name=name,
        protocol=protocol,
        dialect="standard",
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
) -> tuple[dict[str, ProviderProfile], str | None]:
    profiles: dict[str, ProviderProfile] = {}
    raw_profiles = data.get("providers", {})
    if not isinstance(raw_profiles, Mapping):
        raise ConfigurationError("[providers] must be a TOML table")
    for name, raw in raw_profiles.items():
        if not isinstance(name, str) or not isinstance(raw, Mapping):
            raise ConfigurationError("each [providers.<name>] entry must be a TOML table")
        profiles[name] = _native_profile(name, raw)

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
    candidates.extend((state_dir / "config.toml", resolved_cwd / ".neuro-code" / "config.toml"))
    data: dict[str, Any] = {}
    loaded_files: list[Path] = []
    for candidate in candidates:
        if candidate.is_file():
            data = _deep_merge(data, _read_toml(candidate))
            loaded_files.append(candidate)

    providers, cc_default = _profiles_from_data(data)
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

    return AppConfig(
        cwd=resolved_cwd,
        state_dir=state_dir,
        providers=providers,
        default_provider=configured_default,
        selected_provider=selected,
        fallback_providers=fallback_providers,
        loaded_files=tuple(loaded_files),
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
    )
    return replace(config, providers=profiles, selected_provider=selected)
