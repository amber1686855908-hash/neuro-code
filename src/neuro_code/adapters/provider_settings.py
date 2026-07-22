from __future__ import annotations

import asyncio
import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from neuro_code.async_utils import run_blocking
from neuro_code.domain.provider_settings import ManagedProviderProfile, ManagedProviderSettings
from neuro_code.errors import ConfigurationError

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
                api_key=api_keys.get(name),
            )
        )
    raw_default = metadata.get("default_provider")
    if raw_default is not None and not isinstance(raw_default, str):
        raise ConfigurationError(f"managed provider settings {metadata_path} are invalid")
    return ManagedProviderSettings(tuple(profiles), raw_default)


class JsonProviderSettingsStore:
    """Atomic user-scoped provider metadata and credential storage.

    Credentials are kept out of the ordinary configuration file and are written to a
    separate private file. The adapter is intentionally replaceable by a platform
    keychain adapter without changing the TUI or application core.
    """

    def __init__(self, state_dir: Path) -> None:
        self._state_dir = state_dir
        self._metadata_path = state_dir / _METADATA_NAME
        self._credentials_path = state_dir / _CREDENTIALS_NAME
        self._write_lock = asyncio.Lock()

    @property
    def metadata_path(self) -> Path:
        return self._metadata_path

    @property
    def credentials_path(self) -> Path:
        return self._credentials_path

    async def load(self) -> ManagedProviderSettings:
        return await run_blocking(load_managed_provider_settings, self._state_dir)

    async def save_profile(
        self,
        profile: ManagedProviderProfile,
        *,
        make_default: bool = True,
    ) -> ManagedProviderSettings:
        async with self._write_lock:
            return await run_blocking(self._save_profile, profile, make_default)

    def _save_profile(
        self,
        profile: ManagedProviderProfile,
        make_default: bool,
    ) -> ManagedProviderSettings:
        current = load_managed_provider_settings(self._state_dir)
        previous = current.profile(profile.name)
        api_key = profile.api_key or (previous.api_key if previous is not None else None)
        if api_key is None:
            raise ConfigurationError(
                f"managed provider profile {profile.name!r} requires an API key"
            )
        saved = ManagedProviderProfile(
            name=profile.name,
            protocol=profile.protocol,
            model=profile.model.strip(),
            base_url=profile.base_url.strip().rstrip("/"),
            dialect=profile.dialect,
            api_key=api_key,
        )
        profiles = [entry for entry in current.profiles if entry.name != saved.name]
        profiles.append(saved)
        profiles.sort(key=lambda entry: entry.name.casefold())
        default = (
            saved.name
            if make_default or current.default_provider is None
            else current.default_provider
        )
        updated = ManagedProviderSettings(tuple(profiles), default)
        self._save(updated)
        return updated

    async def set_default(self, name: str) -> ManagedProviderSettings:
        async with self._write_lock:
            return await run_blocking(self._set_default, name)

    def _set_default(self, name: str) -> ManagedProviderSettings:
        current = load_managed_provider_settings(self._state_dir)
        if current.profile(name) is None:
            raise ConfigurationError(f"managed provider profile does not exist: {name}")
        updated = ManagedProviderSettings(current.profiles, name)
        self._save(updated)
        return updated

    async def delete_profile(self, name: str) -> ManagedProviderSettings:
        async with self._write_lock:
            return await run_blocking(self._delete_profile, name)

    def _delete_profile(self, name: str) -> ManagedProviderSettings:
        current = load_managed_provider_settings(self._state_dir)
        if current.profile(name) is None:
            raise ConfigurationError(f"managed provider profile does not exist: {name}")
        profiles = tuple(profile for profile in current.profiles if profile.name != name)
        default = current.default_provider
        if default == name:
            default = profiles[0].name if profiles else None
        updated = ManagedProviderSettings(profiles, default)
        self._save(updated)
        return updated

    def _save(self, settings: ManagedProviderSettings) -> None:
        metadata = {
            "version": _SCHEMA_VERSION,
            "default_provider": settings.default_provider,
            "providers": [
                {
                    "name": profile.name,
                    "protocol": profile.protocol,
                    "dialect": profile.dialect,
                    "model": profile.model,
                    "base_url": profile.base_url,
                }
                for profile in settings.profiles
            ],
        }
        credentials = {
            "version": _SCHEMA_VERSION,
            "api_keys": {
                profile.name: profile.api_key
                for profile in settings.profiles
                if profile.api_key is not None
            },
        }
        self._atomic_write(self._credentials_path, credentials)
        self._atomic_write(self._metadata_path, metadata)

    @staticmethod
    def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                json.dump(payload, temporary, ensure_ascii=False, indent=2)
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, path)
            temporary_path = None
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)


__all__ = ["JsonProviderSettingsStore", "load_managed_provider_settings"]
