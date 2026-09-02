"""Bootstrap configuration loading and input normalization.

The value objects and policy contract are owned by
``neuro_code.application.ports.configuration``. This module owns only the
filesystem, environment, legacy-format, and managed-settings loading path.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from neuro_code.application.ports.configuration import (
    AppConfig,
    ProviderProfile,
    _is_loopback_url,
)
from neuro_code.application.ports.lsp import LanguageServerProfile
from neuro_code.application.ports.model import ModelCapabilitySet
from neuro_code.application.ports.provider_dialects import resolve_legacy_dialect
from neuro_code.application.ports.routing import ModelRoute, RuntimeRole
from neuro_code.application.ports.web_fetch import WebFetchMode
from neuro_code.application.ports.web_search import WebSearchMode
from neuro_code.domain.sandbox.models import SandboxProfile
from neuro_code.infrastructure.providers.managed_provider_settings import (
    load_managed_provider_settings as _load_managed_provider_settings,
)
from neuro_code.shared.errors import ConfigurationError

__all__ = ["load_config"]

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


def _web_fetch_mode_from_data(data: Mapping[str, object]) -> WebFetchMode:
    raw = data.get("web_fetch")
    if raw is None:
        return WebFetchMode.DISABLED
    if not isinstance(raw, Mapping):
        raise ConfigurationError("[web_fetch] must be a TOML table")
    mode = _string(raw.get("mode"), WebFetchMode.DISABLED.value)
    try:
        return WebFetchMode(mode)
    except ValueError as error:
        values = ", ".join(item.value for item in WebFetchMode)
        raise ConfigurationError(f"web_fetch mode must be one of: {values}") from error


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


def _language_servers_from_data(data: Mapping[str, object]) -> dict[str, LanguageServerProfile]:
    """Parse only explicit argv-safe LSP profiles; never install or infer one."""

    raw_section = data.get("lsp", data.get("language_servers", {}))
    if raw_section is None:
        return {}
    if not isinstance(raw_section, Mapping):
        raise ConfigurationError("[lsp] must be a TOML table")
    raw_servers = raw_section.get("servers", raw_section)
    if not isinstance(raw_servers, Mapping):
        raise ConfigurationError("[lsp.servers] must be a TOML table")
    profiles: dict[str, LanguageServerProfile] = {}
    for name, raw_profile in raw_servers.items():
        if not isinstance(name, str) or not name.strip():
            raise ConfigurationError("LSP profile names must be non-empty strings")
        if not isinstance(raw_profile, Mapping):
            raise ConfigurationError(f"[lsp.servers.{name}] must be a TOML table")
        raw_command = raw_profile.get("command")
        if (
            not isinstance(raw_command, list)
            or not raw_command
            or any(not isinstance(part, str) or not part for part in raw_command)
        ):
            raise ConfigurationError(
                f"[lsp.servers.{name}] command must be a non-empty TOML argv array"
            )
        raw_extensions = raw_profile.get("extensions", [])
        if not isinstance(raw_extensions, list) or any(
            not isinstance(extension, str) for extension in raw_extensions
        ):
            raise ConfigurationError(f"[lsp.servers.{name}] extensions must be a TOML array")
        raw_markers = raw_profile.get("root_markers", [])
        if not isinstance(raw_markers, list) or any(
            not isinstance(marker, str) for marker in raw_markers
        ):
            raise ConfigurationError(f"[lsp.servers.{name}] root_markers must be a TOML array")
        raw_environment = raw_profile.get("environment", {})
        if not isinstance(raw_environment, Mapping) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in raw_environment.items()
        ):
            raise ConfigurationError(
                f"[lsp.servers.{name}] environment must be a string TOML table"
            )
        enabled = raw_profile.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ConfigurationError(f"[lsp.servers.{name}] enabled must be boolean")
        try:
            profile = LanguageServerProfile(
                name=name,
                language=_string(raw_profile.get("language"), name),
                command=tuple(raw_command),
                extensions=tuple(raw_extensions),
                root_markers=tuple(raw_markers),
                environment=dict(raw_environment),
                enabled=enabled,
            )
        except (TypeError, ValueError) as error:
            raise ConfigurationError(f"invalid LSP profile {name!r}: {error}") from error
        profiles[name] = profile
    return profiles


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
    web_fetch_mode = _web_fetch_mode_from_data(data)
    language_servers = _language_servers_from_data(data)

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
        web_fetch_mode=web_fetch_mode,
        language_servers=language_servers,
    )
