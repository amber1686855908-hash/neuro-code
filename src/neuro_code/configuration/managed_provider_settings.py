"""Synchronous readers for managed provider settings."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from neuro_code.domain.background_tasks import BackgroundTaskWakePolicy
from neuro_code.domain.provider_settings import (
    ManagedProviderProfile,
    ManagedProviderSettings,
    ManagedProxyPolicy,
)
from neuro_code.shared.errors import ConfigurationError

_SCHEMA_VERSION = 1
_METADATA_NAME = "providers.json"
_CREDENTIALS_NAME = "credentials.json"
_MAX_FILE_BYTES = 1_048_576
_SUPPORTED_PROTOCOLS = frozenset(
    {
        "openai-chat",
        "openai-responses",
        "anthropic-messages",
        "gemini-generate-content",
    }
)
_SUPPORTED_DIALECTS = frozenset({"standard", "xai"})


def _read_json(path: Path, *, missing: object) -> object:
    try:
        if path.stat().st_size > _MAX_FILE_BYTES:
            raise ConfigurationError(f"managed provider settings {path} exceed the size limit")
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return missing
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ConfigurationError(
            f"cannot load managed provider settings {path}: {error}"
        ) from error


def _mapping(payload: object, *, path: Path) -> Mapping[str, object]:
    if not isinstance(payload, Mapping) or payload.get("version") != _SCHEMA_VERSION:
        raise ConfigurationError(f"managed provider settings {path} have an unsupported schema")
    return payload


def load_managed_provider_settings(state_dir: Path) -> ManagedProviderSettings:
    """Load bounded metadata and credentials from the user state directory."""

    metadata_path = state_dir / _METADATA_NAME
    credentials_path = state_dir / _CREDENTIALS_NAME
    raw_metadata = _read_json(metadata_path, missing=None)
    if raw_metadata is None:
        return ManagedProviderSettings()
    metadata = _mapping(raw_metadata, path=metadata_path)
    raw_credentials = _read_json(
        credentials_path,
        missing={"version": _SCHEMA_VERSION, "api_keys": {}},
    )
    credentials = _mapping(raw_credentials, path=credentials_path)
    raw_api_keys = credentials.get("api_keys", {})
    if not isinstance(raw_api_keys, Mapping):
        raise ConfigurationError(f"managed provider credentials {credentials_path} are invalid")
    api_keys: dict[str, str] = {}
    for name, value in raw_api_keys.items():
        if not isinstance(name, str) or not isinstance(value, str) or not value.strip():
            raise ConfigurationError(
                f"managed provider credentials {credentials_path} contain an invalid entry"
            )
        api_keys[name] = value.strip()

    raw_profiles = metadata.get("providers", [])
    if not isinstance(raw_profiles, list):
        raise ConfigurationError(f"managed provider settings {metadata_path} are invalid")
    profiles: list[ManagedProviderProfile] = []
    for raw_profile in raw_profiles:
        if not isinstance(raw_profile, Mapping):
            raise ConfigurationError(f"managed provider settings {metadata_path} are invalid")
        values = {
            field: raw_profile.get(field)
            for field in ("name", "protocol", "model", "base_url", "dialect")
        }
        if not all(isinstance(values[field], str) for field in values):
            raise ConfigurationError(f"managed provider settings {metadata_path} are invalid")
        # Version-one files always implied the environment policy when this
        # field was absent. Treat that legacy default as inheritance so the new
        # user-wide setting has the same behavior until the user changes it.
        proxy_mode = raw_profile.get("proxy_mode")
        if proxy_mode == "environment":
            proxy_mode = None
        proxy_url_env = raw_profile.get("proxy_url_env")
        context_window_tokens = raw_profile.get("context_window_tokens")
        raw_wake_policy = raw_profile.get("background_task_wake_policy")
        if (
            (proxy_mode is not None and not isinstance(proxy_mode, str))
            or (proxy_url_env is not None and not isinstance(proxy_url_env, str))
            or (
                context_window_tokens is not None
                and (
                    not isinstance(context_window_tokens, int)
                    or isinstance(context_window_tokens, bool)
                )
            )
            or (raw_wake_policy is not None and not isinstance(raw_wake_policy, str))
        ):
            raise ConfigurationError(f"managed provider settings {metadata_path} are invalid")
        try:
            wake_policy = (
                BackgroundTaskWakePolicy(raw_wake_policy) if raw_wake_policy is not None else None
            )
        except ValueError as error:
            raise ConfigurationError(
                f"managed provider settings {metadata_path} are invalid"
            ) from error
        name = str(values["name"])
        protocol = str(values["protocol"])
        dialect = str(values["dialect"])
        if protocol not in _SUPPORTED_PROTOCOLS or dialect not in _SUPPORTED_DIALECTS:
            raise ConfigurationError(f"managed provider settings {metadata_path} are invalid")
        profiles.append(
            ManagedProviderProfile(
                name=name,
                protocol=protocol,
                model=str(values["model"]),
                base_url=str(values["base_url"]),
                dialect=dialect,
                context_window_tokens=context_window_tokens,
                proxy_mode=proxy_mode,
                proxy_url_env=proxy_url_env,
                api_key=api_keys.get(name),
                background_task_wake_policy=wake_policy,
            )
        )
    raw_default = metadata.get("default_provider")
    if raw_default is not None and not isinstance(raw_default, str):
        raise ConfigurationError(f"managed provider settings {metadata_path} are invalid")
    raw_proxy_defaults = metadata.get("proxy_defaults", {})
    if not isinstance(raw_proxy_defaults, Mapping):
        raise ConfigurationError(f"managed provider settings {metadata_path} are invalid")
    proxy_mode = raw_proxy_defaults.get("mode", "environment")
    proxy_url_env = raw_proxy_defaults.get("proxy_url_env")
    if not isinstance(proxy_mode, str) or (
        proxy_url_env is not None and not isinstance(proxy_url_env, str)
    ):
        raise ConfigurationError(f"managed provider settings {metadata_path} are invalid")
    raw_wake_policy = metadata.get(
        "background_task_wake_policy",
        BackgroundTaskWakePolicy.DISABLED.value,
    )
    if not isinstance(raw_wake_policy, str):
        raise ConfigurationError(f"managed provider settings {metadata_path} are invalid")
    try:
        wake_policy = BackgroundTaskWakePolicy(raw_wake_policy)
    except ValueError as error:
        raise ConfigurationError(
            f"managed provider settings {metadata_path} are invalid"
        ) from error
    return ManagedProviderSettings(
        tuple(profiles),
        raw_default,
        ManagedProxyPolicy(proxy_mode, proxy_url_env),
        wake_policy,
    )


__all__ = ["load_managed_provider_settings"]
