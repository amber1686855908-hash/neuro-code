from __future__ import annotations

import asyncio
import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from neuro_code.application.ports.provider_settings import (
    ManagedProviderProfile,
    ManagedProviderSettings,
    ManagedProxyPolicy,
)
from neuro_code.domain.background_tasks.models import BackgroundTaskWakePolicy
from neuro_code.infrastructure.providers.managed_provider_settings import (
    _CREDENTIALS_NAME,
    _METADATA_NAME,
    _SCHEMA_VERSION,
)
from neuro_code.infrastructure.providers.managed_provider_settings import (
    load_managed_provider_settings as _load_managed_provider_settings,
)
from neuro_code.shared.async_utils import run_blocking
from neuro_code.shared.errors import ConfigurationError


class JsonProviderSettingsStore:
    """Atomic user-scoped provider metadata and credential storage.

    Credentials are kept out of the ordinary configuration file and are written to a
    separate private file. The adapter is intentionally replaceable by a platform
    keychain adapter without changing the TUI or application core.

    提供用户范围 Provider 元数据和凭据的原子存储. 凭据与普通配置文件分离,未来可替换为平台密钥链适配器.
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
        return await run_blocking(_load_managed_provider_settings, self._state_dir)

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
        current = _load_managed_provider_settings(self._state_dir)
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
            service_id=profile.service_id,
            capability_overrides=profile.capability_overrides,
            context_window_tokens=profile.context_window_tokens,
            proxy_mode=profile.proxy_mode,
            proxy_url_env=profile.proxy_url_env,
            api_key=api_key,
            background_task_wake_policy=profile.background_task_wake_policy,
        )
        profiles = [entry for entry in current.profiles if entry.name != saved.name]
        profiles.append(saved)
        profiles.sort(key=lambda entry: entry.name.casefold())
        default = (
            saved.name
            if make_default or current.default_provider is None
            else current.default_provider
        )
        updated = ManagedProviderSettings(
            tuple(profiles),
            default,
            current.proxy_defaults,
            current.background_task_wake_policy,
        )
        self._save(updated)
        return updated

    async def set_default(self, name: str) -> ManagedProviderSettings:
        async with self._write_lock:
            return await run_blocking(self._set_default, name)

    def _set_default(self, name: str) -> ManagedProviderSettings:
        current = _load_managed_provider_settings(self._state_dir)
        if current.profile(name) is None:
            raise ConfigurationError(f"managed provider profile does not exist: {name}")
        updated = ManagedProviderSettings(
            current.profiles,
            name,
            current.proxy_defaults,
            current.background_task_wake_policy,
        )
        self._save(updated)
        return updated

    async def save_proxy_defaults(
        self,
        proxy_defaults: ManagedProxyPolicy,
    ) -> ManagedProviderSettings:
        async with self._write_lock:
            return await run_blocking(self._save_proxy_defaults, proxy_defaults)

    def _save_proxy_defaults(
        self,
        proxy_defaults: ManagedProxyPolicy,
    ) -> ManagedProviderSettings:
        current = _load_managed_provider_settings(self._state_dir)
        updated = ManagedProviderSettings(
            current.profiles,
            current.default_provider,
            proxy_defaults,
            current.background_task_wake_policy,
        )
        self._save(updated)
        return updated

    async def save_background_task_wake_policy(
        self,
        policy: BackgroundTaskWakePolicy,
    ) -> ManagedProviderSettings:
        async with self._write_lock:
            return await run_blocking(self._save_background_task_wake_policy, policy)

    def _save_background_task_wake_policy(
        self,
        policy: BackgroundTaskWakePolicy,
    ) -> ManagedProviderSettings:
        current = _load_managed_provider_settings(self._state_dir)
        updated = ManagedProviderSettings(
            current.profiles,
            current.default_provider,
            current.proxy_defaults,
            policy,
        )
        self._save(updated)
        return updated

    async def delete_profile(self, name: str) -> ManagedProviderSettings:
        async with self._write_lock:
            return await run_blocking(self._delete_profile, name)

    def _delete_profile(self, name: str) -> ManagedProviderSettings:
        current = _load_managed_provider_settings(self._state_dir)
        if current.profile(name) is None:
            raise ConfigurationError(f"managed provider profile does not exist: {name}")
        profiles = tuple(profile for profile in current.profiles if profile.name != name)
        default = current.default_provider
        if default == name:
            default = profiles[0].name if profiles else None
        updated = ManagedProviderSettings(
            profiles,
            default,
            current.proxy_defaults,
            current.background_task_wake_policy,
        )
        self._save(updated)
        return updated

    def _save(self, settings: ManagedProviderSettings) -> None:
        metadata = {
            "version": _SCHEMA_VERSION,
            "default_provider": settings.default_provider,
            "proxy_defaults": {
                "mode": settings.proxy_defaults.mode,
                "proxy_url_env": settings.proxy_defaults.proxy_url_env,
            },
            "background_task_wake_policy": settings.background_task_wake_policy.value,
            "providers": [
                {
                    "name": profile.name,
                    "protocol": profile.protocol,
                    "dialect": profile.dialect,
                    "service_id": profile.service_id,
                    "model": profile.model,
                    "base_url": profile.base_url,
                    "capabilities": profile.capability_overrides.to_mapping(include_unknown=False),
                    "context_window_tokens": profile.context_window_tokens,
                    "proxy_mode": profile.proxy_mode,
                    "proxy_url_env": profile.proxy_url_env,
                    "background_task_wake_policy": (
                        profile.background_task_wake_policy.value
                        if profile.background_task_wake_policy is not None
                        else None
                    ),
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


__all__ = ["JsonProviderSettingsStore"]
